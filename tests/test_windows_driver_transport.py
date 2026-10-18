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
    IOCTL_WRITE_EC,
    DirectEcClient,
    DriverEcTransport,
    open_driver_ec_transport,
)


class FakeKernel32:
    def __init__(self, result: bytes, returned: int = 64, ok: bool = True) -> None:
        self.result, self.returned, self.ok = result, returned, ok
        self.requests: list[tuple[int, int, int, int, int]] = []
        self.closed = []

    def DeviceIoControl(self, handle, code, in_buffer, in_size, out_buffer, out_size, returned, overlapped):
        address = ctypes.c_uint32.from_buffer(in_buffer).value
        self.requests.append((code, address, in_buffer[4], in_size, out_size))
        ctypes.memmove(out_buffer, self.result, len(self.result))
        returned._obj.value = self.returned
        return self.ok

    def CloseHandle(self, handle):
        self.closed.append(handle)


def transport_with(kernel32: FakeKernel32) -> DriverEcTransport:
    transport = DriverEcTransport.__new__(DriverEcTransport)
    transport._kernel32 = kernel32
    transport._handle = 1234
    return transport


class DriverTransportTests(unittest.TestCase):
    def setUp(self):
        # ctypes.get_last_error exists only on Windows; the error path uses it.
        patcher = patch("ctypes.get_last_error", create=True, return_value=1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_sends_only_the_small_read_ioctl_and_returns_the_low_byte(self):
        kernel32 = FakeKernel32(bytes([0x10, 0, 0, 0, 0, 0, 0, 0]))
        transport = transport_with(kernel32)
        self.assertEqual(transport.read(0x0740), 0x10)
        self.assertEqual(kernel32.requests, [(IOCTL_READ_EC, 0x0740, 0, 64, 64)])
        transport.close()
        transport.close()
        self.assertEqual(kernel32.closed, [1234])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            transport.read(0x0740)

    def test_write_puts_the_value_at_offset_four_and_ignores_the_status_word(self):
        # The target driver answered a write with feffffff in the first dword.
        kernel32 = FakeKernel32(bytes.fromhex("feffffff01000000"))
        transport_with(kernel32).write(0x0749, 1)
        self.assertEqual(kernel32.requests, [(IOCTL_WRITE_EC, 0x0749, 1, 64, 64)])
        with self.assertRaisesRegex(RuntimeError, "write"):
            transport_with(FakeKernel32(bytes(8), ok=False)).write(0x0749, 1)

    def test_failed_or_malformed_reads_raise(self):
        for kernel32 in (FakeKernel32(bytes(8), ok=False), FakeKernel32(bytes([1, 1, 0, 0])),
                         FakeKernel32(bytes([1, 0, 0, 0]), returned=2)):
            with self.subTest(result=kernel32.result, ok=kernel32.ok), self.assertRaises(RuntimeError):
                transport_with(kernel32).read(0x0F20)

    def test_driver_transport_is_used_only_for_the_exact_driver_image(self):
        with TemporaryDirectory() as directory:
            driver = Path(directory) / "UWACPIDriver.sys"
            driver.write_bytes(b"other driver")
            opened = Mock()
            with patch.object(direct_fan_control, "UWACPI_DRIVER_PATH", driver), \
                    patch.object(direct_fan_control, "DriverEcTransport", opened):
                self.assertIsNone(open_driver_ec_transport())
                with patch.object(direct_fan_control, "UWACPI_DRIVER_SHA256",
                                  direct_fan_control.hashlib.sha256(b"other driver").hexdigest()):
                    self.assertIs(open_driver_ec_transport(), opened.return_value)
                    opened.side_effect = OSError("busy")
                    self.assertIsNone(open_driver_ec_transport())
                driver.unlink()
                opened.side_effect = None
                self.assertIsNone(open_driver_ec_transport())
            self.assertEqual(opened.call_count, 2)


class ClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.memory = {0x0740: 0x10, 0x0741: 1, 0x0751: 0xA0, 0x07C5: 0xA0, 0x07C6: 5}
        for address in range(0x0F00, 0x0F60):
            self.memory[address] = 100 if 0x0F20 <= address < 0x0F30 or 0x0F50 <= address < 0x0F60 else 0
        self.library = SimpleNamespace(ReadEC=Mock(side_effect=self.memory.__getitem__),
                                       WriteEC=Mock(side_effect=self.memory.__setitem__))
        self.loader = Mock(return_value=self.library)
        # Patching os.name changes it process-wide, so pathlib on Linux would then
        # refuse to build paths; the OEM curve cache lookup is the one that does.
        for target, value in (("hashlib.sha256", Mock(return_value=SimpleNamespace(
                hexdigest=lambda: ACPI_LIBRARY_SHA256))), ("os.name", "nt"),
                              ("load_oem_curve_cache", Mock(return_value=None))):
            patcher = patch(f"backend.direct_fan_control.{target}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(Path, "read_bytes", return_value=b"library")
        patcher.start()
        self.addCleanup(patcher.stop)

    def driver(self) -> Mock:
        return Mock(read=Mock(side_effect=self.memory.__getitem__),
                    write=Mock(side_effect=self.memory.__setitem__))

    def test_driver_carries_reads_and_writes_and_the_library_is_not_loaded(self):
        driver = self.driver()
        client = DirectEcClient(library_loader=self.loader, broker_available=lambda: False,
                                driver_transport=lambda: driver)
        client.connect()
        self.assertEqual(client.transport, "driver_ioctl")
        client.set_boost(True)
        self.loader.assert_not_called()
        driver.write.assert_any_call(0x0751, 0x40)
        self.assertEqual(self.memory[0x0751], 0x40)
        client.close()
        driver.close.assert_called_once()
        self.assertIsNone(client.transport)

    def test_driver_writes_keep_the_per_byte_ownership_check(self):
        driver = self.driver()
        client = DirectEcClient(library_loader=self.loader, driver_transport=lambda: driver,
                                broker_available=lambda: bool(driver.write.call_count))
        client.connect()
        with self.assertRaisesRegex(RuntimeError, "broker"):
            client.set_boost(True)
        driver.write.assert_called_once_with(0x0741, 1)

    def test_library_carries_both_when_no_driver_transport_is_available(self):
        client = DirectEcClient(library_loader=self.loader, broker_available=lambda: False,
                                driver_transport=lambda: None)
        client.connect()
        self.assertEqual(client.transport, "library")
        client.set_boost(True)
        self.assertTrue(self.library.ReadEC.called)
        self.library.WriteEC.assert_any_call(0x0751, 0x40)

    def test_substituted_library_never_opens_the_real_driver_by_default(self):
        with patch("backend.direct_fan_control.open_driver_ec_transport") as real:
            client = DirectEcClient(library_loader=self.loader, broker_available=lambda: False)
            client.connect()
        real.assert_not_called()
        self.assertEqual(client.transport, "library")

    def test_failed_hardware_validation_closes_the_driver_handle(self):
        self.memory[0x0740] = 0x11
        driver = self.driver()
        client = DirectEcClient(library_loader=self.loader, broker_available=lambda: False,
                                driver_transport=lambda: driver)
        with self.assertRaisesRegex(RuntimeError, "project ID"):
            client.connect()
        driver.close.assert_called_once()
        driver.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
