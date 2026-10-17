import threading
import time
import unittest
from unittest.mock import Mock, patch

from backend.fan_control_backend import BackendController
from backend.temperature_service import Temperatures
from shared.bounded_operation import BoundedOperation, OperationBusy, OperationTimeout
from stellaris15gen3 import InProcessBackendClient


HOT = Temperatures(95.0, 85.0, "test", "test")
COOL = Temperatures(45.0, 40.0, "test", "test")


class FaultService:
    method = "test"

    def __init__(self):
        self.calls = []
        self.write_error = None
        self.boost_error = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.stall = False

    def apply_manual(self, cpu, gpu, **kwargs):
        self.calls.append(("target", cpu, gpu))
        if self.stall:
            self.entered.set()
            if not self.release.wait(2):
                raise RuntimeError("test did not release the fake driver")
            self.calls.append(("late_target_finished", cpu, gpu))
        if self.write_error:
            raise RuntimeError(self.write_error)
        return {"cpu": cpu, "gpu": gpu}

    def set_boost(self, enabled):
        self.calls.append(("boost", enabled))
        self.finished.set()
        if self.boost_error:
            raise RuntimeError(self.boost_error)
        return enabled


def controller_with(service):
    controller = BackendController()
    controller._service = service
    controller._hardware_timeout = 0.04
    controller._sensor_timeout = 0.04
    return controller


class OperationSafetyTests(unittest.TestCase):
    def test_ordinary_hardware_contention_serializes_without_false_emergency(self):
        service = FaultService()
        controller = controller_with(service)
        controller._hardware_timeout = 1.0
        entered, release, writer_done = threading.Event(), threading.Event(), threading.Event()
        service.read_telemetry = lambda: (entered.set(), release.wait(2))
        reader = threading.Thread(target=lambda: controller._call_service("read_telemetry"))
        def write():
            controller.apply_manual(50, 50, confirmed_low=False)
            writer_done.set()
        writer = threading.Thread(target=write)
        try:
            reader.start()
            self.assertTrue(entered.wait(1))
            writer.start()
            self.assertFalse(writer_done.wait(0.02))
            self.assertEqual(service.calls, [])
        finally:
            release.set()
            reader.join(2)
            if writer.ident is not None:
                writer.join(2)
        self.assertTrue(writer_done.is_set())
        self.assertEqual(service.calls, [("target", 50, 50)])
        self.assertIsNone(controller._snapshot()["output_error"])

    def test_expired_wait_for_slot_never_executes_later(self):
        lane = BoundedOperation("test")
        entered, release = threading.Event(), threading.Event()
        def owner():
            entered.set()
            release.wait(2)
        thread = threading.Thread(target=lambda: lane.call(owner, 1))
        rejected = Mock()
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(OperationBusy):
                lane.call(rejected, 0.02, wait_for_slot=True)
        finally:
            release.set()
            thread.join(2)
        rejected.assert_not_called()

    def test_deadline_keeps_slot_owned_and_never_queues_rejected_requests(self):
        lane = BoundedOperation("test")
        release, finished = threading.Event(), threading.Event()
        calls = []
        def slow():
            calls.append("old")
            release.wait(2)
        def repair():
            calls.append("repair")
            finished.set()
        try:
            with self.assertRaises(OperationTimeout):
                lane.call(slow, 0.02, after_timeout=repair)
            with self.assertRaises(OperationBusy):
                lane.call(lambda: calls.append("rejected"), 0.02)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(calls, ["old", "repair"])

    def test_success_and_error_propagate_without_abandonment_action(self):
        lane = BoundedOperation("test")
        repair = Mock()
        self.assertEqual(lane.call(lambda: 42, 1, after_timeout=repair), 42)
        with self.assertRaisesRegex(ValueError, "bad"):
            lane.call(Mock(side_effect=ValueError("bad")), 1, after_timeout=repair)
        repair.assert_not_called()
        self.assertEqual(lane.call(lambda: 43, 1), 43)

    def test_stuck_sensor_does_not_block_boost_or_accept_late_freshness(self):
        service = FaultService()
        controller = controller_with(service)
        release, finished = threading.Event(), threading.Event()
        reads = []
        def slow():
            reads.append(1)
            release.wait(2)
            finished.set()
            return COOL
        controller._last_sensor_success = time.monotonic() - 60
        before = controller._last_sensor_success
        try:
            with patch("backend.fan_control_backend.read_temperatures", side_effect=slow):
                with self.assertRaises(OperationTimeout):
                    controller._read_validated_temperatures()
                controller._check_sensors()
                self.assertTrue(controller._snapshot()["sensor_emergency"])
                self.assertTrue(controller._snapshot()["emergency_boost_confirmed"])
                self.assertEqual(service.calls, [("boost", True)])
                self.assertEqual(reads, [1])
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(controller._last_sensor_success, before)

    def test_failed_hot_auto_write_immediately_tries_guarded_boost(self):
        service = FaultService()
        service.write_error = "readback/rollback failed"
        controller = controller_with(service)
        controller.set_mode(True, 35, 75)
        with patch("backend.fan_control_backend.read_temperatures", return_value=HOT):
            controller._run_auto_cycle()
        self.assertEqual(service.calls, [("target", 100, 100), ("boost", True)])
        status = controller._snapshot()
        self.assertIn("rollback", status["output_error"])
        self.assertTrue(status["emergency_boost_confirmed"])
        with self.assertRaisesRegex(RuntimeError, "output failure"):
            controller.apply_manual(50, 50, confirmed_low=False)
        with self.assertRaisesRegex(RuntimeError, "output failure"):
            controller.set_boost(False)

    def test_boost_refusal_is_visible_and_retried_without_claiming_success(self):
        service = FaultService()
        service.write_error = "write failed"
        service.boost_error = "OEM GCUBridge owns EC"
        controller = controller_with(service)
        controller.set_mode(True, 35, 75)
        with patch("backend.fan_control_backend.read_temperatures", return_value=HOT):
            controller._run_auto_cycle()
        status = controller._snapshot()
        self.assertFalse(status["emergency_boost_confirmed"])
        self.assertIn("GCUBridge", status["boost_error"])
        service.boost_error = None
        controller._update_sensor_emergency()
        self.assertTrue(controller._snapshot()["emergency_boost_confirmed"])
        self.assertIsNotNone(controller._snapshot()["output_error"])

    def test_only_verified_fresh_auto_write_clears_output_fault(self):
        service = FaultService()
        service.write_error = "write failed"
        controller = controller_with(service)
        controller.set_mode(True, 35, 75)
        with patch("backend.fan_control_backend.read_temperatures", return_value=HOT):
            controller._run_auto_cycle()
        service.write_error = None
        with patch("backend.fan_control_backend.read_temperatures", side_effect=RuntimeError("sensor failed")):
            controller._run_auto_cycle()
        self.assertIsNotNone(controller._snapshot()["output_error"])
        with patch("backend.fan_control_backend.read_temperatures", return_value=HOT):
            controller._run_auto_cycle()
        self.assertIsNone(controller._snapshot()["output_error"])
        self.assertEqual(service.calls[-1], ("target", 100, 100))

    def test_late_ec_write_is_followed_by_boost_before_owner_is_released(self):
        service = FaultService()
        service.stall = True
        controller = controller_with(service)
        try:
            with self.assertRaises(OperationTimeout):
                controller.apply_manual(50, 60, confirmed_low=False)
            self.assertIsNotNone(controller._snapshot()["output_error"])
            self.assertFalse(controller._snapshot()["emergency_boost_confirmed"])
            with self.assertRaises(OperationBusy):
                controller.set_boost(True)
            with self.assertRaisesRegex(RuntimeError, "output failure"):
                controller.apply_manual(70, 70, confirmed_low=False)
            self.assertEqual(service.calls, [("target", 50, 60)])
        finally:
            service.release.set()
        self.assertTrue(service.finished.wait(1))
        self.assertEqual(service.calls, [
            ("target", 50, 60), ("late_target_finished", 50, 60), ("boost", True)
        ])
        # A successfully completed fallback does not silently clear the fault.
        self.assertIsNotNone(controller._snapshot()["output_error"])

    def test_manual_full_speed_is_a_verified_recovery_path(self):
        service = FaultService()
        controller = controller_with(service)
        controller._mark_output_failure("previous write failed")
        controller.apply_manual(100, 100, confirmed_low=False)
        self.assertIsNone(controller._snapshot()["output_error"])
        controller.apply_manual(50, 50, confirmed_low=False)
        self.assertEqual(service.calls, [("target", 100, 100), ("target", 50, 50)])

    def test_sensor_boost_failure_remains_latched_and_retries(self):
        service = FaultService()
        service.boost_error = "ownership refused"
        controller = controller_with(service)
        controller._last_sensor_success -= 60
        controller._update_sensor_emergency()
        status = controller._snapshot()
        self.assertTrue(status["sensor_emergency"])
        self.assertFalse(status["emergency_boost_confirmed"])
        with self.assertRaisesRegex(RuntimeError, "Sensor failure"):
            controller.set_boost(False)
        service.boost_error = None
        controller._update_sensor_emergency()
        self.assertTrue(controller._snapshot()["emergency_boost_confirmed"])

    def test_load_state_does_not_turn_safety_boost_into_user_boost(self):
        service = FaultService()
        service.load_state = lambda: {"status": {"FanBoostEnable": "1"}}
        controller = controller_with(service)
        controller._mark_output_failure("previous write failed")
        with patch("backend.fan_control_backend.read_temperatures", return_value=HOT), \
                patch("backend.fan_control_backend.read_cpu_power_draw", None), \
                patch("backend.fan_control_backend.read_gpu_power_draw", None):
            controller.load_state()
        self.assertFalse(controller._boost_enabled)

    def test_packaged_deadline_is_effective_without_starting_second_request(self):
        release, finished = threading.Event(), threading.Event()
        def dispatch(command, arguments):
            release.wait(2)
            finished.set()
            return "late reply"
        backend = Mock()
        backend.dispatch.side_effect = dispatch
        client = InProcessBackendClient(backend)
        try:
            with self.assertRaises(OperationTimeout):
                client.request("apply_manual", request_timeout=0.02, cpu=50, gpu=50)
            with self.assertRaises(OperationBusy):
                client.request("set_boost", request_timeout=0.02, enabled=True)
            backend.dispatch.assert_called_once()
            self.assertFalse(client.can_restart)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))


if __name__ == "__main__":
    unittest.main()
