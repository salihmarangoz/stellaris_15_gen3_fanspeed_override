import argparse
import copy
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

if __package__:
    from backend.direct_fan_control import DirectEcClient
else:
    from direct_fan_control import DirectEcClient


HOST = "::1"
PORT = 13688
CLIENT_ID = "MyDynamicDesktop"
USERNAME = "MyDynamicDesktopUser"
PASSWORD = "MyDynamicDesktopPwd888881772688"


class ControlCenterClient:
    method_name = "oem_mqtt"

    def __init__(self) -> None:
        self._connected = threading.Event()
        self._messages: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=32)
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
            protocol=mqtt.MQTTv311,
        )
        self._client.username_pw_set(USERNAME, PASSWORD)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._started = False

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            self._messages.put(("error", f"MQTT connection failed: {reason_code}"))
            return
        client.subscribe("Fan/Status")
        client.subscribe("Fan/Table")
        client.subscribe("System/FanInfo")
        self._connected.set()

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = message.payload.decode("utf-8", errors="replace")
        try:
            self._messages.put_nowait((message.topic, payload))
        except queue.Full:
            self._messages.get_nowait()
            self._messages.put_nowait((message.topic, payload))

    def connect(self) -> None:
        if self._started:
            return
        self._client.connect(HOST, PORT, keepalive=30)
        self._client.loop_start()
        if not self._connected.wait(timeout=5):
            self._client.loop_stop()
            raise TimeoutError("Could not connect to the Control Center MQTT service")
        self._started = True

    def close(self) -> None:
        if not self._started:
            return
        self._client.disconnect()
        self._client.loop_stop()
        self._started = False
        self._connected.clear()

    def __enter__(self) -> "ControlCenterClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def publish(self, payload: dict[str, Any], topic: str = "Fan/Control") -> None:
        result = self._client.publish(topic, json.dumps(payload))
        result.wait_for_publish(timeout=5)

    def _wait_for(self, topic: str, timeout: float = 8) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                received_topic, payload = self._messages.get(
                    timeout=max(0.1, deadline - time.monotonic())
                )
            except queue.Empty:
                break
            if received_topic == "error":
                raise RuntimeError(payload)
            if received_topic == topic and isinstance(payload, dict):
                return payload
        raise TimeoutError(f"No response received on {topic}")

    def status(self) -> dict[str, Any]:
        self.publish({"Action": "GETSTATUS"})
        return self._wait_for("Fan/Status")

    def curve(self, name: str) -> dict[str, Any]:
        self.publish({"Action": "GET_FAN_SPEED_CURVE_SETTING", "Name": name})
        while True:
            curve = self._wait_for("Fan/Table")
            if curve.get("Name") == name:
                return curve

    def set_curve(self, curve: dict[str, Any]) -> None:
        name = curve["Name"]
        for fan_type in ("CPU", "GPU"):
            points = curve[fan_type]
            request = {
                "Action": "SET_FAN_SPEED_CURVE_SETTING",
                "Name": name,
                "Type": fan_type,
            }
            request.update({f"T{point['ID']}": point["Duty"] for point in points})
            self.publish(request)
        self.publish(
            {
                "Action": "SET_FAN_CONTROL_RESPECTIVE",
                "Name": name,
                "FanControlRespective": curve.get("FanControlRespective", False),
            }
        )

    def set_boost(self, enabled: bool) -> None:
        self.publish({"Action": "FAN_BOOST_ON" if enabled else "FAN_BOOST_OFF"})

    def fan_info(self) -> dict[str, Any]:
        self.publish({"Action": "System_ON"}, topic="System/Control")
        try:
            return self._wait_for("System/FanInfo", timeout=10)
        finally:
            self.publish({"Action": "System_OFF"}, topic="System/Control")


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


def current_curve_name(client: ControlCenterClient, requested: str | None) -> str:
    if requested:
        return requested
    status = client.status()
    name = status.get("FAN_TableName")
    if not name:
        raise RuntimeError("Control Center did not report an active fan table")
    return str(name)


def fixed_curve(
    curve: dict[str, Any], cpu_duty: int, gpu_duty: int, include_idle: bool
) -> dict[str, Any]:
    updated = copy.deepcopy(curve)
    updated["FanControlRespective"] = True
    duties = {"CPU": cpu_duty, "GPU": gpu_duty}
    for fan_type, duty in duties.items():
        for point in updated[fan_type]:
            if include_idle or point["ID"] != 0:
                point["Duty"] = duty
    return updated


def save_backup(curve: dict[str, Any]) -> Path:
    if getattr(sys, "frozen", False):
        data_root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        backup_dir = data_root / "StellarisFanControl" / "fan-backups"
    else:
        backup_dir = Path(__file__).resolve().parents[1] / "fan-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"{curve['Name']}-{timestamp}.json"
    path.write_text(json.dumps(curve, indent=2) + "\n", encoding="utf-8")
    return path


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control the XMG/Uniwill fan curve through OEM MQTT or direct EC access."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show the current fan/profile status")
    subparsers.add_parser("rpm", help="Read current fan telemetry")

    curve_parser = subparsers.add_parser("curve", help="Show a fan curve")
    curve_parser.add_argument("--name", help="Table name; defaults to the active table")

    fixed_parser = subparsers.add_parser(
        "fixed", help="Set every active curve point to one duty percentage"
    )
    fixed_parser.add_argument("duty", type=int, choices=range(30, 101), metavar="30-100")
    fixed_parser.add_argument(
        "--gpu-duty",
        type=int,
        choices=range(30, 101),
        metavar="30-100",
        help="Use a different GPU duty; defaults to the CPU duty",
    )
    fixed_parser.add_argument("--name", help="Table name; defaults to the active table")
    fixed_parser.add_argument(
        "--include-idle",
        action="store_true",
        help="Also change point 0, making the fans run below the normal start temperature",
    )
    fixed_parser.add_argument(
        "--apply", action="store_true", help="Apply the curve; otherwise only preview it"
    )

    restore_parser = subparsers.add_parser("restore", help="Restore a saved curve backup")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument(
        "--direct",
        action="store_true",
        help="Restore a DIRECT_EC backup through the validated OEM EC driver",
    )
    restore_parser.add_argument("--apply", action="store_true")

    boost_parser = subparsers.add_parser("boost", help="Turn the OEM 100%% override on or off")
    boost_parser.add_argument("state", choices=("on", "off"))
    boost_parser.add_argument("--apply", action="store_true")
    return parser


def main() -> None:
    args = make_parser().parse_args()
    if args.command == "restore" and args.direct:
        curve = json.loads(args.backup.read_text(encoding="utf-8"))
        print_json(curve)
        if not args.apply:
            print("Dry run only. Add --apply to restore this direct EC backup.", file=sys.stderr)
            return
        with DirectEcClient() as direct_client:
            direct_client.restore_curve(curve)
        print(f"Restored the direct EC fan table from {args.backup}")
        return

    with ControlCenterClient() as client:
        if args.command == "status":
            print_json(client.status())
            return

        if args.command == "rpm":
            print_json(client.fan_info())
            return

        if args.command == "curve":
            print_json(client.curve(current_curve_name(client, args.name)))
            return

        if args.command == "fixed":
            name = current_curve_name(client, args.name)
            existing = client.curve(name)
            gpu_duty = args.gpu_duty if args.gpu_duty is not None else args.duty
            updated = fixed_curve(existing, args.duty, gpu_duty, args.include_idle)
            print_json(updated)
            if not args.apply:
                print("Dry run only. Add --apply to write this curve.", file=sys.stderr)
                return
            backup = save_backup(existing)
            client.set_curve(updated)
            client.set_boost(False)
            print(
                f"Applied CPU {args.duty}% / GPU {gpu_duty}% to {name}. "
                f"Backup: {backup}"
            )
            return

        if args.command == "restore":
            curve = json.loads(args.backup.read_text(encoding="utf-8"))
            if curve.get("Name") == "DIRECT_EC":
                raise RuntimeError("A DIRECT_EC backup must be restored with --direct")
            print_json(curve)
            if not args.apply:
                print("Dry run only. Add --apply to restore this curve.", file=sys.stderr)
                return
            client.set_curve(curve)
            client.set_boost(False)
            print(f"Restored {curve['Name']} from {args.backup}")
            return

        if args.command == "boost":
            if not args.apply:
                print(f"Dry run only: would turn fan boost {args.state}.")
                return
            client.set_boost(args.state == "on")
            print(f"Fan boost turned {args.state}.")


if __name__ == "__main__":
    main()
