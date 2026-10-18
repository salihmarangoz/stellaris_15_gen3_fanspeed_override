import io
import json
import sys
import time
import unittest
from unittest.mock import Mock, patch

from backend import nvidia_helper
from backend.fan_control_backend import BackendController
from backend.nvidia_helper import NvidiaHelper, handle_request, helper_command, serve
from backend.temperature_service import Temperatures, read_nvidia_temperature, read_temperatures


def child(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


ANSWER = (
    "import json, sys\n"
    "print(json.dumps({'ready': True}), flush=True)\n"
    "for line in sys.stdin:\n"
    "    request = json.loads(line)\n"
    "    print(json.dumps({'id': request['id'] - 1, 'ok': True, 'result': {'stale': True}}), flush=True)\n"
    "    print(json.dumps({'id': request['id'], 'ok': True, 'result': {'op': request['op']}}), flush=True)\n"
)
HANG = (
    "import json, sys, time\n"
    "print(json.dumps({'ready': True}), flush=True)\n"
    "sys.stdin.readline()\n"
    "time.sleep(60)\n"
)


class RequestTests(unittest.TestCase):
    def test_powered_off_gpu_is_never_queried(self):
        nvml = Mock()
        for operation in ("temperature", "power", "limits"):
            self.assertEqual(handle_request(nvml, {"op": operation}, lambda: True), {"powered_off": True})
        self.assertEqual(nvml.mock_calls, [])

    def test_working_or_unknown_power_state_queries_nvml(self):
        for state in (False, None, RuntimeError("PnP failed")):
            nvml = Mock()
            nvml.temperature.return_value = {"name": "RTX", "temperature": 50}
            powered_off = Mock(side_effect=state) if isinstance(state, Exception) else Mock(return_value=state)
            with self.subTest(state=state):
                self.assertEqual(handle_request(nvml, {"op": "temperature"}, powered_off)["temperature"], 50)

    def test_failed_query_resets_the_session_and_unknown_operations_are_refused(self):
        nvml = Mock()
        nvml.power.side_effect = RuntimeError("GPU is lost")
        with self.assertRaisesRegex(RuntimeError, "lost"):
            handle_request(nvml, {"op": "power"}, lambda: False)
        nvml.reset.assert_called_once()
        with self.assertRaises(ValueError):
            handle_request(nvml, {"op": "set_power_limit"}, lambda: False)

    def test_serve_answers_each_line_and_reports_errors(self):
        nvml = Mock()
        nvml.limits.return_value = {"base_limit_w": 115}
        requests = io.BytesIO(b'{"id": 1, "op": "limits"}\nnot json\n{"id": 3, "op": "bogus"}\n')
        output = io.BytesIO()
        with patch("backend.nvidia_helper.nvidia_gpu_powered_off", return_value=False):
            self.assertEqual(serve(requests, output, nvml), 0)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(messages[0], {"ready": True})
        self.assertEqual(messages[1], {"id": 1, "ok": True, "result": {"base_limit_w": 115}})
        self.assertFalse(messages[2]["ok"])
        self.assertEqual((messages[3]["id"], messages[3]["ok"]), (3, False))
        nvml.reset.assert_called()


class HelperProcessTests(unittest.TestCase):
    def test_matching_answer_is_returned_and_stale_answers_are_skipped(self):
        helper = NvidiaHelper("test", command=lambda: child(ANSWER))
        self.addCleanup(helper.close)
        self.assertEqual(helper.query("temperature", 10), {"op": "temperature"})
        self.assertEqual(helper.query("power", 10), {"op": "power"})

    def test_hung_query_kills_the_helper_and_the_next_query_starts_a_new_one(self):
        commands = iter([child(HANG), child(ANSWER)])
        helper = NvidiaHelper("test", command=lambda: next(commands))
        self.addCleanup(helper.close)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            helper.query("temperature", 3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(helper._process)
        self.assertEqual(helper.query("temperature", 10), {"op": "temperature"})

    def test_slow_startup_is_not_killed_before_its_limit(self):
        slow = "import time; time.sleep(1.5)\n" + ANSWER
        helper = NvidiaHelper("test", command=lambda: child(slow))
        self.addCleanup(helper.close)
        with self.assertRaisesRegex(TimeoutError, "starting"):
            helper.query("temperature", 0.3)
        process = helper._process
        self.assertIsNotNone(process)
        self.assertEqual(helper.query("temperature", 10), {"op": "temperature"})
        self.assertIs(helper._process, process)

    def test_exited_helper_is_reported_and_replaced(self):
        commands = iter([child("import sys; sys.exit(3)"), child(ANSWER)])
        helper = NvidiaHelper("test", command=lambda: next(commands))
        self.addCleanup(helper.close)
        with self.assertRaisesRegex(RuntimeError, "exited"):
            helper.query("temperature", 10)
        self.assertEqual(helper.query("temperature", 10), {"op": "temperature"})

    def test_source_helper_runs_standalone_in_isolated_mode(self):
        command = helper_command()
        self.assertEqual(command[:2], [sys.executable, "-I"])
        helper = NvidiaHelper("test", command=lambda: command)
        self.addCleanup(helper.close)
        # An unknown operation fails before any NVML or PnP call.
        with self.assertRaisesRegex(RuntimeError, "Unknown NVIDIA helper operation"):
            helper.query("bogus", 10)

    def test_frozen_executable_starts_itself_in_helper_mode(self):
        with patch.object(nvidia_helper.sys, "frozen", True, create=True):
            self.assertEqual(helper_command(), [sys.executable, nvidia_helper.HELPER_ARGUMENT])


class PackagedEntryPointTests(unittest.TestCase):
    def test_packaged_executable_routes_the_helper_argument_to_serve(self):
        import stellaris15gen3

        self.assertEqual(stellaris15gen3.NVIDIA_HELPER_ARGUMENT, nvidia_helper.HELPER_ARGUMENT)
        with patch.object(sys, "argv", ["StellarisFanControl.exe", nvidia_helper.HELPER_ARGUMENT]), \
                patch("backend.nvidia_helper.serve", return_value=0) as run, \
                patch("stellaris15gen3.is_administrator") as administrator:
            with self.assertRaises(SystemExit) as result:
                stellaris15gen3.main()
        self.assertEqual(result.exception.code, 0)
        run.assert_called_once_with()
        administrator.assert_not_called()


class PoweredOffTemperatureTests(unittest.TestCase):
    def test_powered_off_gpu_reports_no_temperature_and_the_cpu_drives_auto(self):
        with patch("backend.temperature_service._temperature_helper.query",
                   return_value={"powered_off": True}):
            self.assertEqual(read_nvidia_temperature(), (None, "NVIDIA GPU powered off (D3)"))
        with patch("backend.temperature_service.read_ryzen_temperature", return_value=(62.0, "CPU")), \
                patch("backend.temperature_service.read_nvidia_temperature",
                      return_value=(None, "off")):
            readings = read_temperatures()
        self.assertEqual(readings, Temperatures(62.0, None, "CPU", "off", True))
        BackendController._validate_temperatures(readings)
        self.assertEqual(BackendController._hottest(readings), 62.0)

    def test_missing_gpu_reading_without_power_off_still_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            BackendController._validate_temperatures(Temperatures(62.0, None, "CPU", "GPU"))
        for result in ({}, {"temperature": "45"}, {"temperature": 121}, {"temperature": 45.5}):
            with self.subTest(result=result), \
                    patch("backend.temperature_service._temperature_helper.query", return_value=result):
                with self.assertRaises(RuntimeError):
                    read_nvidia_temperature()


if __name__ == "__main__":
    unittest.main()
