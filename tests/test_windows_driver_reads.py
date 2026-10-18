import ctypes
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import direct_fan_control
from backend.direct_fan_control import (
    ACPI_LIBRARY_SHA256,
    IOCTL_READ_EC,
    DirectEcClient,
    DriverEcReader,
    open_driver_ec_reader,
)


class FakeKernel32:
    def __init__(self, result: bytes, returned: int = 64, ok: bool = True) -> None:
        self.result, self.returned, self.ok = result, returned, ok
        self.requests: list[tuple[int, int, int, int]] = []
        self.closed = []

    def DeviceIoControl(self, handle, code, in_buffer, in_size, out_buffer, out_size, returned, overlapped):
        address = ctypes.c_uint32.from_buffer(in_buffer).value
        self.requests.append((code, address, in_size, out_size))
        ctypes.memmove(out_buffer, self.result, len(self.result))
        returned._obj.value = self.returned
        return self.ok

    def CloseHandle(self, handle):
        self.closed.append(handle)


def reader_with(kernel32: FakeKernel32) -> DriverEcReader:
    reader = DriverEcReader.__new__(DriverEcReader)
    reader._kernel32 = kernel32
    reader._handle = 1234
    return reader


class DriverReaderTests(unittest.TestCase):
    def test_read_sends_only_the_small_read_ioctl_and_returns_the_low_byte(self):
        kernel32 = FakeKernel32(bytes([0x10, 0, 0, 0, 0, 0, 0, 0]))
        reader = reader_with(kernel32)
        self.assertEqual(reader.read(0x0740), 0x10)
        self.assertEqual(kernel32.requests, [(IOCTL_READ_EC, 0x0740, 64, 64)])
        reader.close()
        reader.close()
        self.assertEqual(kernel32.closed, [1234])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            reader.read(0x0740)

    def test_failed_or_malformed_reads_raise(self):
        for kernel32 in (FakeKernel32(bytes(8), ok=False), FakeKernel32(bytes([1, 1, 0, 0])),
                         FakeKernel32(bytes([1, 0, 0, 0]), returned=2)):
            with self.subTest(result=kernel32.result, ok=kernel32.ok), self.assertRaises(RuntimeError):
                reader_with(kernel32).read(0x0F20)

    def test_driver_reader_is_used_only_for_the_exact_driver_image(self):
        with TemporaryDirectory() as directory:
            driver = Path(directory) / "UWACPIDriver.sys"
            driver.write_bytes(b"other driver")
            opened = Mock()
            with patch.object(direct_fan_control, "UWACPI_DRIVER_PATH", driver), \
                    patch.object(direct_fan_control, "DriverEcReader", opened):
                self.assertIsNone(open_driver_ec_reader())
                with patch.object(direct_fan_control, "UWACPI_DRIVER_SHA256",
                                  direct_fan_control.hashlib.sha256(b"other driver").hexdigest()):
                    self.assertIs(open_driver_ec_reader(), opened.return_value)
                    opened.side_effect = OSError("busy")
                    self.assertIsNone(open_driver_ec_reader())
                driver.unlink()
                opened.side_effect = None
                self.assertIsNone(open_driver_ec_reader())
            self.assertEqual(opened.call_count, 2)


class ClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.memory = {0x0740: 0x10, 0x0741: 1, 0x0751: 0xA0, 0x07C5: 0xA0, 0x07C6: 5}
        for address in range(0x0F00, 0x0F60):
            self.memory[address] = 100 if 0x0F20 <= address < 0x0F30 or 0x0F50 <= address < 0x0F60 else 0
        self.library = SimpleNamespace(ReadEC=Mock(side_effect=self.memory.__getitem__),
                                       WriteEC=Mock(side_effect=self.memory.__setitem__))
        for name, value in (("hashlib.sha256", SimpleNamespace(hexdigest=lambda: ACPI_LIBRARY_SHA256)),
                            ("os.name", "nt")):
            patcher = patch(f"backend.direct_fan_control.{name}", value if name == "os.name"
                            else Mock(return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(Path, "read_bytes", return_value=b"library")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_use_the_driver_and_writes_stay_on_the_library(self):
        reader = Mock(read=Mock(side_effect=self.memory.__getitem__))
        client = DirectEcClient(library_loader=Mock(return_value=self.library),
                                broker_available=lambda: False, driver_reader=lambda: reader)
        client.connect()
        self.assertEqual(client.read_transport, "driver_ioctl")
        client.set_boost(True)
        self.library.ReadEC.assert_not_called()
        self.assertTrue(reader.read.called)
        self.library.WriteEC.assert_any_call(0x0751, 0x40)
        client.close()
        reader.close.assert_called_once()
        self.assertIsNone(client.read_transport)

    def test_library_reads_remain_when_no_driver_reader_is_available(self):
        client = DirectEcClient(library_loader=Mock(return_value=self.library),
                                broker_available=lambda: False, driver_reader=lambda: None)
        client.connect()
        self.assertEqual(client.read_transport, "library")
        self.assertTrue(self.library.ReadEC.called)

    def test_substituted_library_never_opens_the_real_driver_by_default(self):
        with patch("backend.direct_fan_control.open_driver_ec_reader") as real:
            client = DirectEcClient(library_loader=Mock(return_value=self.library),
                                    broker_available=lambda: False)
            client.connect()
        real.assert_not_called()
        self.assertEqual(client.read_transport, "library")

    def test_failed_hardware_validation_closes_the_driver_handle(self):
        self.memory[0x0740] = 0x11
        reader = Mock(read=Mock(side_effect=self.memory.__getitem__))
        client = DirectEcClient(library_loader=Mock(return_value=self.library),
                                broker_available=lambda: False, driver_reader=lambda: reader)
        with self.assertRaisesRegex(RuntimeError, "project ID"):
            client.connect()
        reader.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
