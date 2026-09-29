import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend import linux_ec
from backend.fan_control_backend import BackendController
from backend.linux_ec import (
    AP_OEM_6_ADDRESS,
    AP_OEM_ADDRESS,
    CONTROL_ADDRESSES,
    CPU_DUTY_ADDRESS,
    CTGP_CONTROL_ADDRESS,
    CTGP_OFFSET_ADDRESS,
    FAN_MODE_ADDRESS,
    GPU_DUTY_ADDRESS,
    LIGHTBAR_BLUE_ADDRESS,
    LIGHTBAR_CONTROL_ADDRESS,
    LIGHTBAR_GREEN_ADDRESS,
    LIGHTBAR_RED_ADDRESS,
    TABLE_ADDRESSES,
    TABLE_CONTROL_ADDRESS,
    LinuxEcClient,
    decode_duty,
    encode_duty,
    tuxedo_daemon_running,
    validate_platform,
)
from backend.linux_fan_service import LinuxFanService
from backend.linux_sensors import (
    GpuReader,
    GpuReading,
    NvidiaLimits,
    RaplPowerMeter,
    SensorReadings,
    query_nvidia,
    read_cpu_temperature,
)
from shared.fan_control_common import SENSOR_STALE_TIMEOUT_SECONDS


# Register values read from the target laptop by the read-only probe.
PROBED_EC = {
    0x0740: 0x10,
    0x0741: 0x01,
    0x0743: 0x07,
    0x0744: 0x00,
    0x0745: 0xFF,
    0x0746: 0x19,
    0x0748: 0x80,
    0x0749: 0x00,
    0x074A: 0x00,
    0x074B: 0x00,
    0x0751: 0xA0,
    0x075B: 0x50,
    0x075C: 0x50,
    0x078E: 0x68,
    0x07C5: 0x20,
    0x07C6: 0x00,
    0x0464: 0x08,
    0x0465: 0x54,
    0x046C: 0x08,
    0x046D: 0xA8,
}


class FakeEc:
    def __init__(self) -> None:
        self.memory = dict(PROBED_EC)
        self.writes: list[tuple[int, int]] = []

    def read(self, address: int) -> int:
        return self.memory.get(address, 0)

    def write(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        self.memory[address] = value

    def close(self) -> None:
        pass


def make_client(ec: FakeEc, *, conflict: bool = False) -> LinuxEcClient:
    client = LinuxEcClient(
        transport_factory=lambda: ec,
        platform_check=lambda: None,
        conflict_check=lambda: conflict,
    )
    client.connect()
    return client


class LinuxEcClientTests(unittest.TestCase):
    def test_duties_use_validated_order_and_bit_level_control(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        self.assertTrue(client.apply_duties(60, 70))
        self.assertEqual([ec.memory[CPU_DUTY_ADDRESS + i] for i in range(16)], [120] * 16)
        self.assertEqual([ec.memory[GPU_DUTY_ADDRESS + i] for i in range(16)], [140] * 16)
        self.assertEqual(ec.memory[0x0F00], 115)
        self.assertEqual(ec.memory[0x0F0F], 131)
        self.assertEqual(ec.memory[FAN_MODE_ADDRESS], 0xA0)
        # Bit 7 is added; the firmware's WhisperMode bit 5 is preserved.
        self.assertEqual(ec.memory[TABLE_CONTROL_ADDRESS], 0xA0)
        self.assertEqual(ec.memory[AP_OEM_6_ADDRESS], 0x04)
        written = [address for address, _ in ec.writes]
        last_table = max(i for i, address in enumerate(written) if address in TABLE_ADDRESSES)
        first_control = min(i for i, address in enumerate(written) if address in CONTROL_ADDRESSES)
        self.assertLess(last_table, first_control)

    def test_unchanged_state_is_not_rewritten(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        client.apply_duties(50, 50)
        ec.writes.clear()
        self.assertFalse(client.apply_duties(50, 50))
        self.assertEqual(ec.writes, [])

    def test_zero_duty_uses_the_fan_off_value(self) -> None:
        self.assertEqual(encode_duty(0), 1)
        self.assertEqual(decode_duty(1), 0)
        self.assertEqual(encode_duty(100), 200)
        for invalid in (-1, 101, 50.5, True):
            with self.assertRaises(ValueError):
                encode_duty(invalid)

    def test_boost_keeps_tables(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        client.apply_duties(40, 40)
        client.set_boost(True)
        self.assertEqual(ec.memory[FAN_MODE_ADDRESS], 0x40)
        self.assertEqual(ec.memory[CPU_DUTY_ADDRESS], 80)
        self.assertTrue(client.boost_enabled())

    def test_failed_readback_rolls_back(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        original_read = ec.read

        def stale_read(address: int) -> int:
            if address == CPU_DUTY_ADDRESS and ec.memory.get(address, 0) == 180:
                return 0
            return original_read(address)

        ec.read = stale_read
        with self.assertRaises(RuntimeError):
            client.apply_duties(90, 90)
        self.assertEqual(ec.memory[FAN_MODE_ADDRESS], 0xA0)
        self.assertEqual(ec.memory[AP_OEM_6_ADDRESS], 0x00)
        self.assertEqual(ec.memory.get(CPU_DUTY_ADDRESS, 0), 0)

    def test_running_tccd_blocks_every_write(self) -> None:
        ec = FakeEc()
        client = make_client(ec, conflict=True)
        with self.assertRaisesRegex(RuntimeError, "tccd"):
            client.apply_duties(50, 50)
        with self.assertRaises(RuntimeError):
            client.write_ctgp_offset(10)
        self.assertEqual(ec.writes, [])

    def test_connect_validates_the_ec(self) -> None:
        for address, value in ((0x0740, 0x12), (0x078E, 0x28), (0x0751, 0x77)):
            ec = FakeEc()
            ec.memory[address] = value
            with self.subTest(address=hex(address)):
                with self.assertRaises(RuntimeError):
                    make_client(ec)

    def test_snapshot_restore_round_trip_and_validation(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        snapshot = client.snapshot()
        client.apply_duties(80, 80)
        client.restore(snapshot)
        self.assertEqual(ec.memory[FAN_MODE_ADDRESS], 0xA0)
        self.assertEqual(ec.memory[AP_OEM_6_ADDRESS], 0x00)
        self.assertEqual(ec.memory[CPU_DUTY_ADDRESS], 0)
        bad = json.loads(json.dumps(snapshot))
        bad["Registers"]["0x0740"] = 1
        with self.assertRaises(ValueError):
            client.restore(bad)

    def test_dynamic_boost_toggle_follows_tuxedo_semantics(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        self.assertTrue(client.dynamic_boost_enabled())
        client.write_dynamic_boost(False)
        self.assertEqual(ec.memory[CTGP_CONTROL_ADDRESS], 0x05)
        self.assertFalse(client.read_ctgp_state()["dynamic_boost"])
        client.write_dynamic_boost(True)
        self.assertEqual(ec.memory[CTGP_CONTROL_ADDRESS], 0x07)
        ec.memory[CTGP_CONTROL_ADDRESS] = 0x03
        client.write_dynamic_boost(False)
        self.assertEqual(ec.memory[CTGP_CONTROL_ADDRESS], 0x00)
        ec.memory[CTGP_CONTROL_ADDRESS] = 0x07
        client.write_ctgp_offset(10, dynamic_boost=False)
        self.assertEqual(ec.memory[CTGP_CONTROL_ADDRESS], 0x05)

    def test_ctgp_direct_write_enables_control(self) -> None:
        ec = FakeEc()
        ec.memory[CTGP_CONTROL_ADDRESS] = 0
        client = make_client(ec)
        client.write_ctgp_offset(25)
        self.assertEqual(ec.memory[CTGP_OFFSET_ADDRESS], 25)
        self.assertEqual(ec.memory[CTGP_CONTROL_ADDRESS], 0x07)
        with self.assertRaises(ValueError):
            client.write_ctgp_offset(51)

    def test_lightbar_modes_change_only_the_animation_bit_and_colors(self) -> None:
        ec = FakeEc()
        ec.memory[LIGHTBAR_CONTROL_ADDRESS] = 0x88
        client = make_client(ec)
        self.assertEqual(client.read_lightbar(), {"mode": "rainbow", "color": [0, 0, 0]})
        self.assertTrue(client.write_lightbar("color", (36, 36, 0)))
        # Colors first, then the animation bit, like tuxedo-drivers.
        self.assertEqual(
            [address for address, _ in ec.writes],
            [LIGHTBAR_RED_ADDRESS, LIGHTBAR_GREEN_ADDRESS, LIGHTBAR_CONTROL_ADDRESS],
        )
        self.assertEqual(ec.memory[LIGHTBAR_CONTROL_ADDRESS], 0x08)
        self.assertEqual(client.read_lightbar(), {"mode": "color", "color": [36, 36, 0]})
        ec.writes.clear()
        self.assertFalse(client.write_lightbar("color", (36, 36, 0)))
        self.assertEqual(ec.writes, [])
        # A forced refresh rewrites matching bytes but reports no drift.
        self.assertFalse(client.write_lightbar("color", (36, 36, 0), force=True))
        self.assertEqual(
            [address for address, _ in ec.writes],
            [LIGHTBAR_RED_ADDRESS, LIGHTBAR_GREEN_ADDRESS, LIGHTBAR_BLUE_ADDRESS, LIGHTBAR_CONTROL_ADDRESS],
        )
        client.write_lightbar("rainbow", (1, 2, 3))
        self.assertEqual(ec.memory[LIGHTBAR_CONTROL_ADDRESS], 0x88)
        self.assertEqual(ec.memory[LIGHTBAR_BLUE_ADDRESS], 0)
        client.write_lightbar("off", (36, 36, 0))
        self.assertEqual(ec.memory[LIGHTBAR_CONTROL_ADDRESS], 0x08)
        self.assertEqual(client.read_lightbar(), {"mode": "off", "color": [0, 0, 0]})

    def test_lightbar_values_are_validated_and_tccd_blocks_writes(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        for mode, color in (
            ("blink", (1, 1, 1)),
            ("color", (37, 0, 0)),
            ("color", (-1, 0, 0)),
            ("color", (True, 0, 0)),
            ("color", (1.5, 0, 0)),
            ("color", (1, 1)),
            ("color", None),
        ):
            with self.subTest(mode=mode, color=color):
                with self.assertRaises(ValueError):
                    client.write_lightbar(mode, color)
        self.assertEqual(ec.writes, [])
        blocked = make_client(FakeEc(), conflict=True)
        with self.assertRaisesRegex(RuntimeError, "tccd"):
            blocked.write_lightbar("off", (0, 0, 0))

    def test_fan_info_decodes_rpm_and_rejects_implausible(self) -> None:
        ec = FakeEc()
        client = make_client(ec)
        info = client.fan_info()
        self.assertEqual((info["CpuFanDuty"], info["CpuFanRpm"], info["GpuFanRpm"]), (40.0, 2132, 2216))
        self.assertFalse(info["FanAbnormal"])
        ec.memory[0x0464] = 0xFF
        self.assertIsNone(client.fan_info()["CpuFanRpm"])


class LinuxPlatformTests(unittest.TestCase):
    def test_platform_gate_checks_dmi_and_dsdt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "board_name").write_text("GMxZGxx\n")
            (root / "product_sku").write_text("STELLARIS1XA03\n")
            dsdt = root / "DSDT"
            dsdt.write_bytes(b"other firmware")
            with self.assertRaisesRegex(RuntimeError, "DSDT"):
                validate_platform(root, dsdt)
            with patch.object(
                linux_ec, "EXPECTED_DSDT_SHA256", hashlib.sha256(b"other firmware").hexdigest()
            ):
                validate_platform(root, dsdt)
            (root / "board_name").write_text("OTHER\n")
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                validate_platform(root, dsdt)

    def test_tccd_detection_uses_the_process_name(self) -> None:
        with TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "12").mkdir()
            (proc / "12" / "cmdline").write_bytes(b"/usr/bin/python3\0tccd-notes.py\0")
            self.assertFalse(tuxedo_daemon_running(proc))
            (proc / "34").mkdir()
            (proc / "34" / "cmdline").write_bytes(b"/opt/tcc/data/service/tccd\0--start\0")
            self.assertTrue(tuxedo_daemon_running(proc))


class LinuxSensorTests(unittest.TestCase):
    def make_sysfs(self, root: Path, *, runtime: str = "active") -> None:
        hwmon = root / "class" / "hwmon" / "hwmon6"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("k10temp\n")
        (hwmon / "temp1_label").write_text("Tctl\n")
        (hwmon / "temp1_input").write_text("54500\n")
        gpu = root / "bus" / "pci" / "devices" / "0000:01:00.0"
        (gpu / "power").mkdir(parents=True)
        (gpu / "vendor").write_text("0x10de\n")
        (gpu / "class").write_text("0x030000\n")
        (gpu / "power" / "runtime_status").write_text(runtime + "\n")
        audio = root / "bus" / "pci" / "devices" / "0000:01:00.1"
        audio.mkdir(parents=True)
        (audio / "vendor").write_text("0x10de\n")
        (audio / "class").write_text("0x040300\n")

    def test_k10temp_tctl_is_read_and_validated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_sysfs(root)
            self.assertEqual(read_cpu_temperature(root)[0], 54.5)
            (root / "class" / "hwmon" / "hwmon6" / "temp1_input").write_text("0\n")
            with self.assertRaises(RuntimeError):
                read_cpu_temperature(root)

    def test_suspended_gpu_is_cold_and_never_queried(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_sysfs(root, runtime="suspended")

            def forbidden() -> GpuReading:
                raise AssertionError("A suspended GPU must not be woken")

            reading = GpuReader(sysfs=root, query=forbidden).read()
            self.assertTrue(reading.powered_off)
            self.assertIsNone(reading.temperature)

    def test_active_gpu_is_queried_and_missing_gpu_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_sysfs(root)
            expected = GpuReading(55.0, "NVIDIA", False, 40.0, NvidiaLimits(115.0, 165.0, 115.0))
            reader = GpuReader(sysfs=root, query=lambda: expected)
            self.assertEqual(reader.read(), expected)
            self.assertEqual(reader.last_limits().max_limit_w, 165.0)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "No NVIDIA GPU"):
                GpuReader(sysfs=Path(directory), query=lambda: expected).read()

    def test_nvidia_smi_output_is_parsed_strictly(self) -> None:
        def runner(output: str):
            return lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, "")

        reading = query_nvidia(runner("NVIDIA GeForce RTX 3080 Laptop GPU, 52, [N/A], 115.00, 165.00, 115.00\n"))
        self.assertEqual(reading.temperature, 52.0)
        self.assertIsNone(reading.power_w)
        self.assertEqual(reading.limits.default_limit_w, 115.0)
        for output in ("GPU, [N/A], 1, 2, 3, 4\n", "GPU, 0, 1, 2, 3, 4\n", "GPU, 52\n", ""):
            with self.subTest(output=output):
                with self.assertRaises(RuntimeError):
                    query_nvidia(runner(output))

    def test_rapl_power_handles_first_sample_and_wraparound(self) -> None:
        with TemporaryDirectory() as directory:
            zone = Path(directory)
            (zone / "name").write_text("package-0\n")
            (zone / "max_energy_range_uj").write_text("1000000\n")
            times = iter([10.0, 12.0])
            meter = RaplPowerMeter(zone, clock=lambda: next(times))
            (zone / "energy_uj").write_text("900000\n")
            self.assertIsNone(meter.sample())
            (zone / "energy_uj").write_text("99999\n")
            self.assertAlmostEqual(meter.sample(), 0.1)


class FakeLinuxService:
    method = "direct_ec"

    def __init__(self) -> None:
        self.writes: list[tuple[int, int, bool]] = []
        self.boost: list[bool] = []

    def apply_manual(self, cpu: int, gpu: int, *, create_backup: bool = True) -> dict:
        self.writes.append((cpu, gpu, create_backup))
        return {"cpu": cpu, "gpu": gpu, "backup": None, "table": "LINUX_EC"}

    def set_boost(self, enabled: bool) -> bool:
        self.boost.append(enabled)
        return enabled

    def set_lightbar(self, mode: str, color: tuple) -> dict:
        return {"available": True, "mode": mode, "color": list(color)}

    def close(self, wait: bool = True) -> None:
        del wait


def controller_with(service: FakeLinuxService) -> BackendController:
    controller = BackendController()
    controller._service = service
    return controller


class ControllerSafetyTests(unittest.TestCase):
    def test_lightbar_commands_need_platform_support(self) -> None:
        controller = controller_with(FakeLinuxService())
        state = controller.dispatch(
            "set_lightbar", {"mode": "color", "red": 1, "green": 2, "blue": 3}
        )
        self.assertEqual(state["color"], [1, 2, 3])
        with self.assertRaisesRegex(RuntimeError, "not available"):
            controller.dispatch("read_lightbar", {})

    def test_powered_off_gpu_lets_auto_use_the_cpu_alone(self) -> None:
        service = FakeLinuxService()
        controller = controller_with(service)
        controller.set_mode(True, 40, 80)
        readings = SensorReadings(60.0, None, "cpu", "gpu off", gpu_powered_off=True)
        with patch("backend.fan_control_backend.read_temperatures", return_value=readings):
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [(65, 65, True)])

    def test_missing_gpu_reading_still_fails_closed(self) -> None:
        service = FakeLinuxService()
        controller = controller_with(service)
        controller.set_mode(True, 40, 80)
        readings = SensorReadings(60.0, None, "cpu", "gpu", gpu_powered_off=False)
        with patch("backend.fan_control_backend.read_temperatures", return_value=readings):
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [])

    def test_stale_sensors_force_boost_only_after_timeout(self) -> None:
        service = FakeLinuxService()
        controller = controller_with(service)
        controller.set_mode(True, 40, 80)
        with patch("backend.fan_control_backend.read_temperatures", side_effect=RuntimeError("gone")):
            controller._check_sensors()
            self.assertEqual(service.boost, [])
            controller._last_sensor_success -= SENSOR_STALE_TIMEOUT_SECONDS
            controller._check_sensors()
        self.assertEqual(service.boost, [True])
        snapshot = controller._snapshot()
        self.assertTrue(snapshot["sensor_emergency"])
        self.assertEqual(snapshot["sensor_error"], "gone")
        with self.assertRaisesRegex(RuntimeError, "Sensor failure"):
            controller.apply_manual(50, 50, confirmed_low=False)
        with self.assertRaisesRegex(RuntimeError, "Sensor failure"):
            controller.set_boost(False)

    def test_recovery_in_auto_resumes_validated_targets(self) -> None:
        service = FakeLinuxService()
        controller = controller_with(service)
        controller.set_mode(True, 40, 80)
        controller._sensor_emergency = True
        readings = SensorReadings(60.0, 50.0, "cpu", "gpu")
        with patch("backend.fan_control_backend.read_temperatures", return_value=readings):
            controller._check_sensors()
            self.assertFalse(controller._sensor_emergency)
            controller._run_auto_cycle()
        self.assertEqual(service.writes, [(65, 65, True)])

    def test_recovery_in_manual_restores_last_duties(self) -> None:
        service = FakeLinuxService()
        controller = controller_with(service)
        controller.apply_manual(45, 55, confirmed_low=False)
        controller._sensor_emergency = True
        readings = SensorReadings(60.0, 50.0, "cpu", "gpu")
        with patch("backend.fan_control_backend.read_temperatures", return_value=readings):
            controller._check_sensors()
        self.assertEqual(service.writes[-1], (45, 55, False))
        self.assertFalse(controller._sensor_emergency)


class FakeServiceClient:
    method_name = "direct_ec"

    def __init__(self) -> None:
        self.duties: list[tuple[int, int]] = []
        self.boost_calls = 0
        self.drifted = False
        self.ctgp = 0
        self.boost_on = True
        self.lightbar = {"mode": "rainbow", "color": [0, 0, 0]}
        self.lightbar_writes: list[tuple[str, tuple[int, int, int], bool]] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def apply_duties(self, cpu: int, gpu: int) -> bool:
        self.duties.append((cpu, gpu))
        drifted, self.drifted = self.drifted, False
        return drifted

    def set_boost(self, enabled: bool) -> bool:
        self.boost_calls += 1
        return True

    def read_ctgp_offset(self) -> int:
        return self.ctgp

    def read_ctgp_state(self) -> dict:
        return {"offset": self.ctgp, "dynamic_boost": self.boost_on, "dynamic_boost_w": 25}

    def dynamic_boost_enabled(self) -> bool:
        return self.boost_on

    def write_dynamic_boost(self, enabled: bool) -> bool:
        self.boost_on = enabled
        return True

    def refuse_conflicting_writer(self) -> None:
        pass

    def write_ctgp_offset(self, watts: int, *, dynamic_boost: bool | None = None) -> bool:
        self.ctgp = watts
        if dynamic_boost is not None:
            self.boost_on = dynamic_boost
        return True

    def snapshot(self) -> dict:
        return {"Name": "LINUX_EC", "Registers": {}}

    def read_lightbar(self) -> dict:
        return dict(self.lightbar)

    def write_lightbar(self, mode: str, color: tuple[int, int, int], *, force: bool = False) -> bool:
        self.lightbar_writes.append((mode, color, force))
        shown = {"mode": mode, "color": list(color) if mode == "color" else [0, 0, 0]}
        if mode == "rainbow":
            shown["color"] = self.lightbar["color"]
        changed = shown != self.lightbar
        self.lightbar = shown
        return changed


class LinuxFanServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        LinuxFanService._instance = None
        self.state = TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"STELLARIS15GEN3_STATE_DIR": self.state.name})
        self.environment.start()
        self.client = FakeServiceClient()
        self.service = LinuxFanService.instance()
        self.service._client_factory = lambda: self.client
        self.service._limits = lambda: NvidiaLimits(115.0, 165.0, 115.0)
        self.service._ctgp_sysfs = lambda: None
        self.service._powerd_running = lambda: True

    def tearDown(self) -> None:
        LinuxFanService._instance = None
        self.environment.stop()
        self.state.cleanup()

    def test_manual_write_backs_up_and_maintain_repairs_drift(self) -> None:
        result = self.service.apply_manual(55, 65)
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual(self.service.maintain(), [])
        self.client.drifted = True
        self.assertEqual(self.service.maintain(), ["fan table"])
        self.assertEqual(self.client.duties[-1], (55, 65))

    def test_leaving_boost_without_a_target_uses_full_speed(self) -> None:
        self.service.set_boost(True)
        self.assertEqual(self.service.maintain(), ["Fan Boost"])
        self.service.set_boost(False)
        self.assertEqual(self.client.duties[-1], (100, 100))

    def test_gpu_offset_is_validated_written_and_maintained(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_gpu_power_offset(55)
        state = self.service.set_gpu_power_offset(20)
        self.assertEqual((state["offset"], state["max_offset"], state["base_limit_w"]), (20, 50, 115.0))
        self.client.ctgp = 0
        self.assertEqual(self.service.maintain(), ["GPU power limit"])
        self.assertEqual(self.client.ctgp, 20)

    def test_dynamic_boost_is_set_reported_and_maintained(self) -> None:
        state = self.service.set_dynamic_boost(False)
        self.assertEqual((state["dynamic_boost"], state["powerd_running"]), (False, True))
        self.client.boost_on = True
        self.assertEqual(self.service.maintain(), ["Dynamic Boost"])
        self.assertFalse(self.client.boost_on)

    def test_lightbar_is_set_reported_and_maintained(self) -> None:
        self.assertEqual(self.service.lightbar_state()["mode"], "rainbow")
        self.assertEqual(self.service.maintain(), [])
        self.assertEqual(self.client.lightbar_writes, [])
        with self.assertRaises(ValueError):
            self.service.set_lightbar("color", (40, 0, 0))
        state = self.service.set_lightbar("off", (36, 20, 0))
        # Off shows nothing but keeps the chosen color for the next Color pick.
        self.assertEqual((state["mode"], state["color"], state["max_level"]), ("off", [36, 20, 0], 36))
        self.assertEqual(self.client.lightbar["color"], [0, 0, 0])
        self.client.lightbar = {"mode": "rainbow", "color": [0, 0, 0]}
        self.assertEqual(self.service.maintain(), ["lightbar"])
        self.assertEqual(self.client.lightbar["mode"], "off")

    def test_lightbar_is_rewritten_every_15_minutes(self) -> None:
        from backend.linux_fan_service import LIGHTBAR_REFRESH_SECONDS

        now = [1000.0]
        self.service._clock = lambda: now[0]
        self.service.set_lightbar("color", (36, 36, 0))
        self.assertEqual(self.client.lightbar_writes[-1][2], True)
        self.assertEqual(self.service.maintain(), [])
        self.assertEqual(self.client.lightbar_writes[-1][2], False)
        now[0] += LIGHTBAR_REFRESH_SECONDS - 1
        self.service.maintain()
        self.assertEqual(self.client.lightbar_writes[-1][2], False)
        now[0] += 1
        # A routine refresh is not reported as drift.
        self.assertEqual(self.service.maintain(), [])
        self.assertEqual(self.client.lightbar_writes[-1][2], True)
        self.service.maintain()
        self.assertEqual(self.client.lightbar_writes[-1][2], False)

    def test_restored_lightbar_is_rewritten_at_the_first_check(self) -> None:
        self.service.set_desired_lightbar("rainbow", (0, 0, 0))
        self.service.maintain()
        self.assertEqual(self.client.lightbar_writes, [("rainbow", (0, 0, 0), True)])

    def test_gpu_offset_prefers_the_kernel_sysfs_attribute(self) -> None:
        attribute = Path(self.state.name) / "ctgp_offset"
        attribute.write_text("0\n")

        def write_through_driver(path: Path) -> tuple[Path, str]:
            return path, "uniwill-laptop sysfs"

        self.service._ctgp_sysfs = lambda: write_through_driver(attribute)
        self.client.read_ctgp_offset = lambda: int(attribute.read_text())
        self.service.set_gpu_power_offset(10)
        self.assertEqual(attribute.read_text(), "10\n")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux service only")
class PeerCredentialIpcTests(unittest.TestCase):
    def run_server(self, allowed_uids: frozenset[int]):
        from backend.linux_backend import PeerCredentialServer

        class PingController:
            def dispatch(self, command: str, arguments: dict) -> str:
                return "pong"

        directory = TemporaryDirectory()
        path = Path(directory.name) / "backend.sock"
        server = PeerCredentialServer(path, PingController(), allowed_uids)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return directory, path, server, thread

    def test_only_allowed_users_are_served(self) -> None:
        from shared.fan_control_ipc import BackendClient

        for allowed, expected in ((frozenset({os.getuid()}), True), (frozenset({0, 65534}), False)):
            if os.getuid() == 0 and not expected:
                continue
            directory, path, server, thread = self.run_server(allowed)
            try:
                self.assertEqual(oct(path.stat().st_mode & 0o777), "0o666")
                with patch.dict(os.environ, {"STELLARIS15GEN3_SOCKET": str(path)}):
                    client = BackendClient(timeout=2.0, transport="unix")
                    if expected:
                        self.assertTrue(client.ping())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "not allowed"):
                            client.ping()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2.0)
                directory.cleanup()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux service only")
class LinuxServiceSettingsTests(unittest.TestCase):
    def test_settings_are_validated_and_persisted(self) -> None:
        from backend.linux_backend import LinuxBackendController, load_allowed_uids, load_settings

        LinuxFanService._instance = None
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"minimum_temp": 90, "maximum_temp": 20, "gpu_power_offset": True, "dynamic_boost": "no", "lightbar_mode": "color", "lightbar_color": [40, 0, 0]}))
            settings = load_settings(path)
            self.assertEqual((settings["minimum_temp"], settings["maximum_temp"]), (35, 75))
            self.assertIsNone(settings["gpu_power_offset"])
            self.assertIsNone(settings["dynamic_boost"])
            self.assertIsNone(settings["lightbar_mode"])
            controller = LinuxBackendController(path)
            controller.configure_auto(40, 70)
            self.assertEqual(json.loads(path.read_text())["maximum_temp"], 70)
            client = FakeServiceClient()
            controller._service._client_factory = lambda: client
            controller.set_lightbar("color", (36, 36, 0))
            saved = json.loads(path.read_text())
            self.assertEqual((saved["lightbar_mode"], saved["lightbar_color"]), ("color", [36, 36, 0]))
            LinuxFanService._instance = None
            restored = LinuxBackendController(path)
            self.assertEqual(restored._service._desired_lightbar, ("color", (36, 36, 0)))
            config = Path(directory) / "config.json"
            config.write_text('{"allowed_uids": [1000]}')
            self.assertEqual(load_allowed_uids(config), frozenset({0, 1000}))
            config.write_text('{"allowed_uids": ["root"]}')
            self.assertEqual(load_allowed_uids(config), frozenset({0}))
        LinuxFanService._instance = None


if __name__ == "__main__":
    unittest.main()
