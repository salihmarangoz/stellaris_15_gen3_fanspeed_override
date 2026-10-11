import ctypes
import struct
import unittest
from unittest.mock import Mock, patch

from backend import temperature_service as sensors


class RyzenPowerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=1.0)
        self.read = Mock(return_value=(0xA1003, 100))
        self.meter = sensors.RyzenPackagePowerMeter(read=self.read, clock=self.clock)

    def sample(self, counter, when, units=0xA1003):
        self.clock.return_value = when
        self.read.return_value = (units, counter)
        return self.meter.sample()

    def test_first_sample_and_energy_delta(self):
        self.assertIsNone(self.sample(100, 1))
        self.assertEqual(self.sample(100 + 20 * 65536, 3), 10.0)

    def test_unsigned_counter_wrap_and_rapid_samples(self):
        self.assertIsNone(self.sample(0xFFFFFFFF - 65535, 1))
        self.assertEqual(self.sample(65536, 2), 2.0)
        self.read.reset_mock()
        self.assertEqual(self.sample(999999, 2.1), 2.0)
        self.read.assert_not_called()

    def test_reset_after_failures_changed_units_or_long_gap(self):
        self.sample(1, 1)
        self.clock.return_value = 2
        self.read.side_effect = OSError("unavailable")
        self.assertIsNone(self.meter.sample())
        self.read.side_effect = None
        self.assertIsNone(self.sample(100, 3))
        self.assertIsNone(self.sample(200, 4, units=0xA1103))
        self.assertIsNone(self.sample(1000, 60, units=0xA1103))
        self.assertIsNone(self.sample(1001, 59, units=0xA1103))

    def test_rejects_invalid_units_counter_and_implausible_power(self):
        for units, counter in ((0, 1), (0xFFFFFFFFFFFFFFFF, 1), (0xA1003, -1),
                               (0xA1003, 0x100000000), ("bad", 1), (0xA1003, True)):
            with self.subTest(units=units, counter=counter):
                self.assertIsNone(self.sample(counter, 1, units=units))
        self.sample(0, 1)
        self.assertIsNone(self.sample(151 * 65536, 2))

    def test_refuses_writes_and_non_sensor_registers_before_opening_device(self):
        with patch.object(sensors.ctypes, "WinDLL", create=True) as library:
            for operation, offsets in (("ioctl_write_msr", (sensors.RYZEN_POWER_UNIT_MSR,)),
                                       ("ioctl_read_msr", (0xC001029A,)),
                                       ("ioctl_read_smn", (0x59954,)),
                                       ("ioctl_read_msr", ())):
                with self.assertRaises(ValueError):
                    sensors._read_ryzen_registers(operation, offsets)
            library.assert_not_called()

    def test_pawnio_batch_uses_only_read_ioctl_and_releases_mutex(self):
        kernel = Mock()
        kernel.CreateFileW.return_value = 101
        kernel.CreateMutexW.return_value = 102
        kernel.WaitForSingleObject.return_value = 0
        requests = []

        def device_io(handle, code, request, length, output, output_length, returned, overlapped):
            if code == sensors.PAWNIO_LOAD_BINARY:
                return 1
            self.assertEqual(code, sensors.PAWNIO_EXECUTE)
            operation = request.raw[:32].rstrip(b'\0')
            offset = struct.unpack('<q', request.raw[32:40])[0]
            requests.append((operation, offset))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_int64))[0] = 0xA1003 if len(requests) == 1 else 123
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32))[0] = 8
            return 1

        kernel.DeviceIoControl.side_effect = device_io
        with patch.object(sensors.ctypes, 'WinDLL', return_value=kernel, create=True), patch(
            'backend.temperature_service._pawn_module_path', return_value=Mock(read_bytes=Mock(return_value=b'pinned module'))
        ):
            self.assertEqual(sensors._read_ryzen_package_energy(), (0xA1003, 123))
        self.assertEqual(requests, [(b'ioctl_read_msr', 0xC0010299), (b'ioctl_read_msr', 0xC001029B)])
        kernel.ReleaseMutex.assert_called_once_with(102)
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_smn_transport_and_temperature_decode_are_preserved(self):
        raw = 400 << 21
        for value, expected in ((raw, 50.0), (raw | (1 << 19), 1.0),
                                (raw | (3 << 16), 1.0)):
            with patch.object(sensors, '_read_ryzen_registers', return_value=(value,)) as read:
                self.assertEqual(sensors.read_ryzen_temperature()[0], expected)
                read.assert_called_once_with('ioctl_read_smn', (0x59800,))

    def test_short_pawnio_result_releases_mutex_and_handles(self):
        kernel = Mock()
        kernel.CreateFileW.return_value = 101
        kernel.CreateMutexW.return_value = 102
        kernel.WaitForSingleObject.return_value = 0

        def device_io(handle, code, request, length, output, output_length, returned, overlapped):
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32))[0] = 4
            return 1

        kernel.DeviceIoControl.side_effect = device_io
        with patch.object(sensors.ctypes, 'WinDLL', return_value=kernel, create=True), patch(
            'backend.temperature_service._pawn_module_path', return_value=Mock(read_bytes=Mock(return_value=b'pinned module'))
        ):
            with self.assertRaisesRegex(RuntimeError, '4-byte'):
                sensors._read_ryzen_package_energy()
        kernel.ReleaseMutex.assert_called_once_with(102)
        self.assertEqual(kernel.CloseHandle.call_count, 2)


if __name__ == '__main__':
    unittest.main()
