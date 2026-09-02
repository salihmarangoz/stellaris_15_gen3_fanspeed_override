import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fan_control_backend import BackendController, BackendServer
from fan_control_common import auto_target
from fan_control_ipc import BackendClient
from temperature_service import Temperatures


class FakeService:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, bool]] = []
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


class BackendSafetyTests(unittest.TestCase):
    def test_auto_target_is_shared_and_reaches_full_speed_by_80(self) -> None:
        self.assertEqual(auto_target(40, 40, 100), 30)
        self.assertEqual(auto_target(80, 40, 100), 100)
        self.assertEqual(auto_target(95, 40, 100), 100)

    def test_auto_cycle_backs_up_once_and_writes_same_target(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        controller.set_mode(True, 40, 80)
        temperatures = Temperatures(65.0, 70.0, "CPU test", "GPU test")
        with patch("fan_control_backend.read_temperatures", return_value=temperatures):
            controller._run_auto_cycle()
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [(85, 85, True), (85, 85, False)])

    def test_backend_scheduler_runs_without_a_frontend(self) -> None:
        controller = BackendController()
        service = FakeService()
        controller._service = service
        temperatures = Temperatures(65.0, 70.0, "CPU test", "GPU test")
        with patch("fan_control_backend.read_temperatures", return_value=temperatures):
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
        with patch("fan_control_backend.read_temperatures", return_value=temperatures):
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
            "fan_control_backend.read_temperatures",
            side_effect=leave_auto_during_read,
        ):
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [])

    def test_frontend_restart_attempts_obey_cooldown(self) -> None:
        controller = BackendController()
        with patch("fan_control_backend.launch_component") as launch:
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


class IpcTests(unittest.TestCase):
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
