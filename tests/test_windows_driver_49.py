import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from backend.direct_fan_control import ACPI_LIBRARY_49_SHA256, DirectEcClient

class Driver49Tests(unittest.TestCase):
    def setUp(self):
        self.registers = {0x740: 0x10, 0x741: 1, 0x751: 0, 0x78e: 0x40, 0x7c5: 0x80, 0x7c6: 0}
        for address in range(0xf00, 0xf60):
            self.registers[address] = 100 if 0xf20 <= address < 0xf30 or 0xf50 <= address < 0xf60 else 0
        self.library = SimpleNamespace(ReadEC=Mock(side_effect=self.registers.__getitem__), WriteEC=Mock(side_effect=lambda address, value: self.registers.__setitem__(address, value)))
        self.loader = Mock(return_value=self.library)
        self.client = DirectEcClient(library_loader=self.loader, broker_available=lambda: False)
        self.hash_patch = patch('backend.direct_fan_control.hashlib.sha256', return_value=SimpleNamespace(hexdigest=lambda: ACPI_LIBRARY_49_SHA256))
        self.read_patch = patch.object(Path, 'read_bytes', return_value=b'vendor image')
        self.platform_patch = patch('backend.direct_fan_control.validate_ctgp_platform')
        self.hash_patch.start(); self.read_patch.start(); self.platform = self.platform_patch.start()
        self.addCleanup(self.hash_patch.stop); self.addCleanup(self.read_patch.stop); self.addCleanup(self.platform_patch.stop)

    def test_49_accepts_observed_inactive_state_and_reports_actual_digest(self):
        self.client.connect()
        self.platform.assert_called_once()
        self.library.WriteEC.assert_not_called()
        self.assertEqual(self.client.curve('DIRECT_EC')['DirectControl']['library_sha256'], ACPI_LIBRARY_49_SHA256)
        self.client.close()

    def test_49_refuses_wrong_platform_before_loading_dll(self):
        self.platform.side_effect = RuntimeError('wrong BIOS')
        with self.assertRaisesRegex(RuntimeError, 'BIOS'):
            self.client.connect()
        self.loader.assert_not_called()

    def test_49_refuses_unknown_state_or_missing_universal_support(self):
        for address, value in ((0x78e, 0), (0x7c6, 255), (0x740, 17)):
            with self.subTest(address=address):
                previous = self.registers[address]
                self.registers[address] = value
                with self.assertRaises(RuntimeError):
                    self.client.connect()
                self.registers[address] = previous
                self.library.WriteEC.assert_not_called()

    def test_unknown_library_is_refused_before_loading(self):
        with patch('backend.direct_fan_control.hashlib.sha256', return_value=SimpleNamespace(hexdigest=lambda: 'unknown')):
            with self.assertRaisesRegex(RuntimeError, 'validated'):
                self.client.connect()
        self.loader.assert_not_called()

    def test_fan_write_stops_before_next_byte_when_broker_returns(self):
        self.client.connect()
        self.client._broker_available = lambda: bool(self.library.WriteEC.call_count)
        with self.assertRaisesRegex(RuntimeError, 'broker'):
            self.client.set_boost(True)
        self.library.WriteEC.assert_called_once_with(0x741, 1)

    def test_49_preserves_other_subsystem_and_mirror_bits_for_curve_and_boost(self):
        self.registers[0x7c6] = 3
        self.registers[0x7c5] = 0xa0
        self.client.connect()
        curve = self.client.curve('DIRECT_EC')
        self.client.set_curve(curve)
        self.assertEqual(self.registers[0x7c6], 7)
        self.assertEqual(self.registers[0x7c5], 0xa0)
        self.client.set_boost(True)
        self.assertEqual(self.registers[0x751], 0x40)
        self.assertEqual(self.registers[0x7c6], 7)
        self.assertEqual(self.registers[0x7c5], 0xa0)
        self.client.set_boost(False)
        self.assertEqual(self.registers[0x751], 0xa0)
        self.assertEqual(self.registers[0x7c6], 7)
