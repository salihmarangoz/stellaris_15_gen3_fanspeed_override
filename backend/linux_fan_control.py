import argparse
import json
from pathlib import Path
from typing import Any

from shared.fan_control_ipc import linux_socket_path
from backend.linux_backend import acquire_process_lock
from backend.linux_ec import (
    CONTROL_ADDRESSES,
    CTGP_ADDRESSES,
    LIGHTBAR_ADDRESSES,
    DevMemEcWindow,
    LinuxEcClient,
    tuxedo_daemon_running,
)
from backend.linux_fan_service import LinuxFanService, find_ctgp_sysfs, save_backup
from backend.linux_sensors import read_temperatures


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


def read_only_client() -> LinuxEcClient:
    client = LinuxEcClient(transport_factory=lambda: DevMemEcWindow(writable=False))
    client.connect()
    return client


def probe() -> None:
    client = read_only_client()
    try:
        registers = {
            f"0x{address:04X}": f"0x{value:02X}"
            for address, value in client.read_registers(
                CONTROL_ADDRESSES + CTGP_ADDRESSES + LIGHTBAR_ADDRESSES
            ).items()
        }
        curve = client.curve()
        print_json(
            {
                "registers": registers,
                "cpu_table_duty": [point["Duty"] for point in curve["CPU"]],
                "gpu_table_duty": [point["Duty"] for point in curve["GPU"]],
                "fan_info": client.fan_info(),
                "ctgp_sysfs": str(find_ctgp_sysfs()[0]) if find_ctgp_sysfs() else None,
                "tccd_running": tuxedo_daemon_running(),
            }
        )
    finally:
        client.close()
    try:
        print_json({"sensors": read_temperatures().__dict__})
    except Exception as exc:
        print_json({"sensor_error": str(exc)})


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Low-level Linux EC fan control. Writes are dry runs unless --apply is given."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="Read-only register, table, fan and sensor report")
    fixed = commands.add_parser("fixed", help="Set one duty for every zone of each fan")
    fixed.add_argument("duty", type=int, choices=range(30, 101), metavar="30-100")
    fixed.add_argument("--gpu-duty", type=int, choices=range(30, 101), metavar="30-100")
    fixed.add_argument("--apply", action="store_true")
    boost = commands.add_parser("boost", help="Enable or leave EC Fan Boost (100%%)")
    boost.add_argument("state", choices=("on", "off"))
    boost.add_argument("--apply", action="store_true")
    restore = commands.add_parser("restore", help="Restore a LINUX_EC-*.json backup")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--apply", action="store_true")
    gpu_power = commands.add_parser("gpu-power", help="Set the NVIDIA cTGP offset in watts")
    gpu_power.add_argument("watts", type=int)
    gpu_power.add_argument("--apply", action="store_true")
    return parser


def main() -> None:
    args = make_parser().parse_args()
    if args.command == "probe":
        probe()
        return
    if not args.apply:
        print("Dry run. Planned change:")
        print_json({key: str(value) for key, value in vars(args).items()})
        print("Add --apply to write it.")
        return

    # Refuse to write while the service owns the EC.
    lock = acquire_process_lock(linux_socket_path().parent)
    try:
        if args.command == "fixed":
            client = LinuxEcClient()
            client.connect()
            try:
                print("Backup:", save_backup(client.snapshot()))
                gpu = args.gpu_duty if args.gpu_duty is not None else args.duty
                client.apply_duties(args.duty, gpu)
                print_json(client.fan_info())
            finally:
                client.close()
        elif args.command == "boost":
            client = LinuxEcClient()
            client.connect()
            try:
                if args.state == "on":
                    client.set_boost(True)
                else:
                    curve = client.curve()
                    client.apply_duties(curve["CPU"][0]["Duty"], curve["GPU"][0]["Duty"])
                print_json(client.fan_info())
            finally:
                client.close()
        elif args.command == "restore":
            snapshot = json.loads(args.backup.read_text(encoding="utf-8"))
            client = LinuxEcClient()
            client.connect()
            try:
                print("Backup:", save_backup(client.snapshot()))
                client.restore(snapshot)
                print("Restored", args.backup)
            finally:
                client.close()
        elif args.command == "gpu-power":
            read_temperatures()
            print_json(LinuxFanService.instance().set_gpu_power_offset(args.watts))
    finally:
        lock.close()


if __name__ == "__main__":
    main()
