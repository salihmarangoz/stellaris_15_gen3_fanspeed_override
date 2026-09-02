import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.direct_fan_control import (
    AP_EXISTS_ADDRESS,
    CPU_TABLE_DUTY_ADDRESS,
    CPU_TEMP_DOWN_ADDRESS,
    CPU_TEMP_UP_ADDRESS,
    EXPECTED_PROJECT_ID,
    FAN_CONTROL_ADDRESS,
    FAN_CONTROL_MIRROR_ADDRESS,
    FAN_CONTROL_USER_HIGH,
    FAN_SUBSYSTEM_STATE_ADDRESS,
    GPU_TABLE_DUTY_ADDRESS,
    GPU_TEMP_DOWN_ADDRESS,
    GPU_TEMP_UP_ADDRESS,
    PROJECT_ID_ADDRESS,
    DirectEcClient,
)
from backend.fan_control_service import ControlCenterService
from backend.fan_control_backend import BackendController, BackendServer, run_backend
from backend.temperature_service import Temperatures
from shared.fan_control_common import auto_target
from shared.fan_control_common import DEFAULT_MAX_FAN_TEMP, DEFAULT_MIN_FAN_TEMP
from shared.fan_control_ipc import BackendClient, component_command, launch_component


class FakeService:
    def __init__(self) -> None:
        self.method = "test"
        self.writes: list[tuple[int, int, bool]] = []
        self.service_changes: list[bool] = []
        self.write_event = threading.Event()

    def apply_manual(
        self, cpu: int, gpu: int, *, create_backup: bool = True
    ) -> dict[str, object]:
        self.writes.append((cpu, gpu, create_backup))
        self.write_event.set()
        return {"cpu": cpu, "gpu": gpu, "backup": None, "table": "test"}

    def close(self, wait: bool = True) -> None:
        del wait

    def set_boost(self, enabled: bool) -> bool:
        return enabled

    def set_oem_service_running(self, enabled: bool) -> bool:
        self.service_changes.append(enabled)
        self.method = "oem_mqtt" if enabled else "direct_ec"
        return enabled

    def load_state(self) -> dict[str, object]:
        return {
            "status": {
                "FAN_TableName": "test",
                "FanMode": 0,
                "FanModeStr": "Normal",
                "FanBoostEnable": "0",
            },
            "curve": {
                "CPU": [{"Duty": 50}],
                "GPU": [{"Duty": 50}],
            },
            "telemetry": {"CPU": 50, "GPU": 50},
        }


class BackendSafetyTests(unittest.TestCase):
    def test_auto_target_is_shared_and_reaches_full_speed_by_80(self) -> None:
        self.assertEqual((DEFAULT_MIN_FAN_TEMP, DEFAULT_MAX_FAN_TEMP), (35, 75))
        self.assertEqual(auto_target(35), 30)
        self.assertEqual(auto_target(75), 100)
        self.assertEqual(auto_target(40, 40, 100), 30)
        self.assertEqual(auto_target(80, 40, 100), 100)
        self.assertEqual(auto_target(95, 40, 100), 100)

    def test_auto_cycle_backs_up_once_and_writes_same_target(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        controller.set_mode(True, 40, 80)
        temperatures = Temperatures(65.0, 70.0, "CPU test", "GPU test")
        with patch(
            "backend.fan_control_backend.read_temperatures", return_value=temperatures
        ):
            controller._run_auto_cycle()
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [(85, 85, True), (85, 85, False)])

    def test_backend_scheduler_runs_without_a_frontend(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        temperatures = Temperatures(65.0, 70.0, "CPU test", "GPU test")
        with patch(
            "backend.fan_control_backend.read_temperatures", return_value=temperatures
        ):
            controller.start(start_frontend=False)
            controller.set_mode(True, 40, 80)
            self.assertTrue(service.write_event.wait(2.0))
            controller.stop()
        self.assertEqual(service.writes, [(85, 85, True)])

    def test_auto_cycle_fails_closed_on_zero_temperature(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        controller.set_mode(True, 40, 80)
        temperatures = Temperatures(0.0, 70.0, "CPU test", "GPU test")
        with patch(
            "backend.fan_control_backend.read_temperatures", return_value=temperatures
        ):
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [])
        self.assertIn("zero", controller._snapshot()["auto_error"].lower())

    def test_switching_to_manual_during_sensor_read_prevents_write(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        controller.set_mode(True, 40, 80)

        def leave_auto_during_read() -> Temperatures:
            controller.set_mode(False, 40, 80)
            return Temperatures(65.0, 70.0, "CPU test", "GPU test")

        with patch(
            "backend.fan_control_backend.read_temperatures",
            side_effect=leave_auto_during_read,
        ):
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [])

    def test_frontend_restart_attempts_obey_cooldown(self) -> None:
        controller = BackendController()
        with patch("backend.fan_control_backend.launch_component") as launch:
            self.assertTrue(controller.show_frontend(force=True))
            self.assertFalse(controller.show_frontend())
        launch.assert_called_once_with("frontend")

    def test_backend_rejects_unconfirmed_low_manual_duty(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        with self.assertRaises(PermissionError):
            controller.apply_manual(25, 50, confirmed_low=False)
        controller.apply_manual(25, 50, confirmed_low=True)
        self.assertEqual(service.writes, [(25, 50, True)])

    def test_backend_requires_confirmation_to_change_oem_service(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        with self.assertRaises(PermissionError):
            controller.dispatch(
                "set_oem_service", {"enabled": False, "confirmed": False}
            )
        result = controller.dispatch(
            "set_oem_service", {"enabled": False, "confirmed": True}
        )
        self.assertEqual(service.service_changes, [False])
        self.assertEqual(result["backend"]["control_method"], "direct_ec")

    def test_confirmed_exit_sets_both_fans_to_80_and_stops_auto(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        controller._automatic = True
        with self.assertRaises(PermissionError):
            controller.dispatch("prepare_exit", {"confirmed": False})
        result = controller.dispatch("prepare_exit", {"confirmed": True})
        self.assertEqual(service.writes, [(80, 80, True)])
        self.assertEqual((result["cpu"], result["gpu"]), (80, 80))
        self.assertFalse(controller._snapshot()["automatic"])


class DirectEcTests(unittest.TestCase):
    def make_client(self) -> tuple[DirectEcClient, dict[int, int], list[tuple[int, int]]]:
        memory = {
            PROJECT_ID_ADDRESS: EXPECTED_PROJECT_ID,
            AP_EXISTS_ADDRESS: 1,
            FAN_CONTROL_ADDRESS: FAN_CONTROL_USER_HIGH,
            FAN_CONTROL_MIRROR_ADDRESS: FAN_CONTROL_USER_HIGH,
            FAN_SUBSYSTEM_STATE_ADDRESS: 5,
        }
        for address in (
            CPU_TEMP_UP_ADDRESS,
            CPU_TEMP_DOWN_ADDRESS,
            GPU_TEMP_UP_ADDRESS,
            GPU_TEMP_DOWN_ADDRESS,
        ):
            for offset in range(16):
                memory[address + offset] = 0 if offset == 0 else 50 + offset
        for address in (CPU_TABLE_DUTY_ADDRESS, GPU_TABLE_DUTY_ADDRESS):
            for offset in range(16):
                memory[address + offset] = 100
        writes: list[tuple[int, int]] = []
        client = DirectEcClient(broker_available=lambda: False)
        client._library = object()
        client._read_ec = lambda address: memory.get(address, 100)

        def write(address: int, value: int) -> None:
            writes.append((address, value))
            memory[address] = value

        client._write_ec = write
        return client, memory, writes

    def test_direct_curve_decodes_oem_half_percent_units(self) -> None:
        client, _, _ = self.make_client()
        curve = client.curve("DIRECT_EC")
        self.assertEqual(curve["CPU"][0]["Duty"], 50)
        self.assertEqual(curve["GPU"][15]["Duty"], 50)

    def test_direct_telemetry_accepts_odd_half_percent_ramp_values(self) -> None:
        client, memory, _ = self.make_client()
        memory[0x075B] = 131
        memory[0x075C] = 149
        self.assertEqual(client.fan_info()["CpuFanDuty"], 65.5)
        self.assertEqual(client.fan_info()["GpuFanDuty"], 74.5)

    def test_direct_write_encodes_both_complete_tables_and_verifies(self) -> None:
        client, memory, writes = self.make_client()
        curve = client.curve("DIRECT_EC")
        for point in curve["CPU"]:
            point["Duty"] = 65
        for point in curve["GPU"]:
            point["Duty"] = 75
        client.set_curve(curve)
        self.assertEqual(
            [memory[CPU_TABLE_DUTY_ADDRESS + offset] for offset in range(16)],
            [130] * 16,
        )
        self.assertEqual(
            [memory[GPU_TABLE_DUTY_ADDRESS + offset] for offset in range(16)],
            [150] * 16,
        )
        self.assertEqual(memory[FAN_CONTROL_ADDRESS], FAN_CONTROL_USER_HIGH)
        self.assertEqual(memory[FAN_CONTROL_MIRROR_ADDRESS], FAN_CONTROL_USER_HIGH)
        self.assertEqual(memory[AP_EXISTS_ADDRESS], 1)
        self.assertEqual(memory[FAN_SUBSYSTEM_STATE_ADDRESS], 5)
        self.assertEqual(len(writes), 100)

    def test_direct_write_refuses_to_race_a_live_oem_broker(self) -> None:
        client, _, writes = self.make_client()
        client._broker_available = lambda: True
        with self.assertRaises(RuntimeError):
            client.set_curve(client.curve("DIRECT_EC"))
        self.assertEqual(writes, [])

    def test_direct_write_rolls_back_after_failed_readback(self) -> None:
        client, memory, _ = self.make_client()
        curve = client.curve("DIRECT_EC")
        for point in curve["CPU"] + curve["GPU"]:
            point["Duty"] = 70
        original_read = client._read_ec
        failed = False

        def stale_read(address: int) -> int:
            nonlocal failed
            if address == CPU_TABLE_DUTY_ADDRESS and memory[address] == 140 and not failed:
                failed = True
                return 100
            return original_read(address)

        client._read_ec = stale_read
        with self.assertRaises(RuntimeError):
            client.set_curve(curve)
        self.assertEqual(
            [memory[CPU_TABLE_DUTY_ADDRESS + offset] for offset in range(16)],
            [100] * 16,
        )
        self.assertEqual(
            [memory[GPU_TABLE_DUTY_ADDRESS + offset] for offset in range(16)],
            [100] * 16,
        )


class MethodSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        ControlCenterService._instance = None

    def tearDown(self) -> None:
        instance = ControlCenterService._instance
        if instance is not None:
            instance.close()
        ControlCenterService._instance = None

    def test_oem_mqtt_is_selected_while_broker_is_running(self) -> None:
        class Client:
            method_name = "oem_mqtt"
            def connect(self): pass
            def close(self): pass

        with (
            patch("backend.fan_control_service.oem_broker_available", return_value=True),
            patch("backend.fan_control_service.ControlCenterClient", Client),
        ):
            service = ControlCenterService.instance()
            self.assertIsInstance(service._connected_client(), Client)
            self.assertEqual(service.method, "oem_mqtt")

    def test_direct_ec_is_selected_while_broker_is_stopped(self) -> None:
        class Client:
            method_name = "direct_ec"
            def connect(self): pass
            def close(self): pass

        with (
            patch("backend.fan_control_service.oem_broker_available", return_value=False),
            patch("backend.fan_control_service.DirectEcClient", Client),
        ):
            service = ControlCenterService.instance()
            self.assertIsInstance(service._connected_client(), Client)
            self.assertEqual(service.method, "direct_ec")

    def test_service_stop_waits_for_broker_and_oem_cleanup(self) -> None:
        service = ControlCenterService.instance()
        with (
            patch("backend.fan_control_service._set_windows_service_running") as change,
            patch("backend.fan_control_service._wait_for_oem_broker") as wait,
            patch("backend.fan_control_service.time.sleep") as sleep,
        ):
            self.assertFalse(service.set_oem_service_running(False))
        change.assert_called_once_with(False)
        wait.assert_called_once_with(False)
        sleep.assert_called_once_with(5.0)


class IpcTests(unittest.TestCase):
    def test_frozen_component_launches_resolve_to_the_single_executable(self) -> None:
        with (
            patch("shared.fan_control_ipc.sys.frozen", True, create=True),
            patch(
                "shared.fan_control_ipc.sys.executable",
                str(Path("C:/FanControl/StellarisFanControl.exe")),
            ),
        ):
            frontend = component_command("frontend")
            backend = component_command("backend", "--no-frontend")
        self.assertEqual(Path(frontend[0]).name, "StellarisFanControl.exe")
        self.assertEqual(frontend[1:], [])
        self.assertEqual(Path(backend[0]).name, "StellarisFanControl.exe")
        self.assertEqual(backend[1:], ["--no-frontend"])

    def test_source_components_use_separate_entry_points(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            frontend = component_command("frontend")
            backend = component_command("backend")
        self.assertEqual(Path(frontend[1]).name, "stellaris15gen3_frontend.py")
        self.assertEqual(Path(backend[1]).name, "stellaris15gen3_backend.py")

    def test_normal_frontend_requests_an_elevated_backend(self) -> None:
        expected = component_command("backend", "--no-frontend")
        with (
            patch("shared.fan_control_ipc.os.name", "nt"),
            patch("shared.fan_control_ipc.is_administrator", return_value=False),
            patch("shared.fan_control_ipc._launch_elevated") as elevated,
        ):
            self.assertIsNone(launch_component("backend", "--no-frontend"))
        elevated.assert_called_once_with(expected)

    def test_elevated_backend_requests_a_normal_frontend(self) -> None:
        expected = component_command("frontend")
        with (
            patch("shared.fan_control_ipc.os.name", "nt"),
            patch("shared.fan_control_ipc.is_administrator", return_value=True),
            patch("shared.fan_control_ipc._launch_unelevated") as unelevated,
        ):
            self.assertIsNone(launch_component("frontend"))
        unelevated.assert_called_once_with(expected)

    def test_backend_refuses_to_run_without_administrator_access(self) -> None:
        with patch(
            "backend.fan_control_backend.is_administrator", return_value=False
        ):
            with self.assertRaises(PermissionError):
                run_backend(start_frontend=False)

    def test_authenticated_loopback_request(self) -> None:
        class PingController:
            def dispatch(self, command: str, arguments: dict[str, object]) -> str:
                self.command = command
                self.arguments = arguments
                return "pong"

        with TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"STELLARIS15GEN3_RUNTIME_DIR": directory}
            ):
                controller = PingController()
                server = BackendServer(controller, "test-token")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                endpoint = Path(directory) / "backend-endpoint.json"
                endpoint.write_text(
                    json.dumps(
                        {
                            "host": server.server_address[0],
                            "port": server.server_address[1],
                            "token": "test-token",
                        }
                    ),
                    encoding="utf-8",
                )
                try:
                    self.assertTrue(BackendClient(timeout=1.0).ping())
                    self.assertEqual(controller.command, "ping")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
