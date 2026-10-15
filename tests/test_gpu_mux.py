import json
import os
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.gpu_mux import (
    DISPLAY_MODE_OFFSET, EXPECTED_SSID, FirmwareMux, LinuxFirmwareTransport,
    VARIABLE_ATTRIBUTES, VARIABLE_SIZE,
)


def firmware_blob(mode=0):
    data = bytearray((index * 17) % 256 for index in range(VARIABLE_SIZE))
    struct.pack_into("<I", data, 0, EXPECTED_SSID)
    data[6], data[DISPLAY_MODE_OFFSET] = 0x10, mode
    return bytes(data)


class FakeFirmware:
    def __init__(self):
        self.data = firmware_blob()
        self.attributes = VARIABLE_ATTRIBUTES
        self.writes = []
        self.ignore_write = False

    def read(self):
        return self.attributes, self.data

    def write(self, attributes, data):
        self.writes.append((attributes, data))
        if not self.ignore_write:
            self.data = data


class FirmwareMuxTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.io = FakeFirmware()
        self.platform = Mock()
        self.conflict = Mock(return_value=False)
        self.mux = FirmwareMux(transport=self.io, platform_check=self.platform,
                               conflict_check=self.conflict, backups=Path(self.directory.name))

    def save(self, mode="dgpu", **overrides):
        args = {"confirmed": True, "confirmation_text": "YES DO IT", **overrides}
        return self.mux.set_mode(mode, **args)

    def test_read_and_construction_never_write(self):
        self.assertEqual(self.io.writes, [])
        self.assertEqual(self.mux.read_state()["mode"], "hybrid")
        self.assertEqual(self.io.writes, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_only_display_byte_changes_preserves_attributes_and_saves_backup(self):
        before = self.io.data
        result = self.save()
        self.assertTrue(result["reboot_required"])
        self.assertEqual(result["mode"], "dgpu")
        self.assertEqual(len(self.io.writes), 1)
        self.assertEqual(self.io.writes[0][0], VARIABLE_ATTRIBUTES)
        self.assertEqual([i for i in range(len(before)) if before[i] != self.io.data[i]], [0x62])
        backup = json.loads(Path(result["backup"]).read_text())
        self.assertEqual(bytes.fromhex(backup["data_hex"]), before)
        self.save()
        self.assertEqual(len(self.io.writes), 1)  # unchanged selection cannot wear NVRAM
        self.save("hybrid")
        self.assertEqual(self.io.data, before)

    def test_both_confirmations_and_exact_phrase_are_required(self):
        for confirmed, text in ((False, "YES DO IT"), (1, "YES DO IT"),
                                (True, "yes do it"), (True, "YES DO IT "),
                                (True, " YES DO IT"), (True, "YES DO IT\n"),
                                (True, ""), (True, None), (True, True)):
            with self.subTest(confirmed=confirmed, text=text):
                with self.assertRaises(PermissionError):
                    self.save(confirmed=confirmed, confirmation_text=text)
        self.assertEqual(self.io.writes, [])
        self.platform.assert_not_called()

    def test_unknown_platform_format_or_mode_is_refused(self):
        for attributes, data in ((3, firmware_blob()), (7, b"short"), (7, firmware_blob(255)),
                                 (7, b"\0" * VARIABLE_SIZE)):
            with self.subTest(attributes=attributes, size=len(data)):
                self.io.attributes, self.io.data = attributes, data
                self.assertFalse(self.mux.read_state()["available"])
                with self.assertRaises(RuntimeError):
                    self.save()
        self.assertEqual(self.io.writes, [])
        self.io.data = firmware_blob()
        self.platform.side_effect = RuntimeError("wrong BIOS")
        with self.assertRaisesRegex(RuntimeError, "BIOS"):
            self.save()
        self.assertEqual(self.io.writes, [])

    def test_conflicting_writer_and_concurrent_firmware_changes_refused(self):
        self.conflict.return_value = True
        self.assertFalse(self.mux.read_state()["write_available"])
        with self.assertRaisesRegex(RuntimeError, "GCUBridge"):
            self.save()
        self.conflict.return_value = False
        original_read = self.io.read
        calls = 0
        def changing_read():
            nonlocal calls
            calls += 1
            if calls == 2:
                data = bytearray(self.io.data)
                data[9] ^= 1
                self.io.data = bytes(data)
            return original_read()
        self.io.read = changing_read
        with self.assertRaisesRegex(RuntimeError, "concurrently"):
            self.save()
        self.assertEqual(self.io.writes, [])

    def test_writer_returning_immediately_before_write_is_refused(self):
        self.conflict.side_effect = [False, False, True]
        with self.assertRaisesRegex(RuntimeError, "GCUBridge"):
            self.save()
        self.assertEqual(self.io.writes, [])

    def test_failed_verification_does_not_blindly_retry_firmware_writes(self):
        self.io.ignore_write = True
        with self.assertRaisesRegex(RuntimeError, "readback"):
            self.save()
        self.assertEqual(len(self.io.writes), 1)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.json"))), 1)

    def test_backup_failure_prevents_write(self):
        with patch.object(self.mux, "_backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.io.writes, [])

    def test_linux_bios_version_must_match_even_when_dsdt_matches(self):
        from backend.gpu_mux import validate_platform
        with patch("backend.gpu_mux.os.name", "posix"), patch(
            "backend.linux_ec.validate_platform"
        ) as validate_ec, patch("backend.gpu_mux.Path.read_text", return_value="N.1.61A15\n"):
            validate_platform()
            validate_ec.assert_called_once()
        with patch("backend.gpu_mux.os.name", "posix"), patch(
            "backend.linux_ec.validate_platform"
        ), patch("backend.gpu_mux.Path.read_text", return_value="another BIOS\n"):
            with self.assertRaisesRegex(RuntimeError, "BIOS"):
                validate_platform()

    def test_linux_efivarfs_attribute_prefix_is_preserved(self):
        path = Path(self.directory.name) / "variable"
        before = struct.pack("<I", 7) + firmware_blob()
        path.write_bytes(before)
        io = LinuxFirmwareTransport(path)
        attrs, data = io.read()
        changed = bytearray(data)
        changed[DISPLAY_MODE_OFFSET] = 1
        io.write(attrs, bytes(changed))
        after = path.read_bytes()
        self.assertEqual([i for i in range(len(before)) if before[i] != after[i]], [4 + 0x62])
        path.write_bytes(b"bad")
        with self.assertRaises(RuntimeError):
            io.read()


class MuxGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_exact_text_is_required_by_second_confirmation_dialog(self):
        from frontend.fan_control_gui import ExperimentalMuxConfirmation
        dialog = ExperimentalMuxConfirmation(None)
        try:
            for text, enabled in (("", False), ("yes do it", False), ("YES DO IT ", False),
                                  (" YES DO IT", False), ("YES DO IT", True)):
                dialog.confirmation_text.setText(text)
                self.assertEqual(dialog.confirm_button.isEnabled(), enabled)
        finally:
            dialog.deleteLater()

    def test_cancel_at_either_stage_never_calls_backend(self):
        from PySide6.QtWidgets import QMessageBox, QDialog
        from frontend.fan_control_gui import FanControlWindow
        for first, second, text, expected in (
            (QMessageBox.StandardButton.Cancel, QDialog.DialogCode.Accepted, "YES DO IT", False),
            (QMessageBox.StandardButton.Yes, QDialog.DialogCode.Rejected, "YES DO IT", False),
            (QMessageBox.StandardButton.Yes, QDialog.DialogCode.Accepted, "yes do it", False),
            (QMessageBox.StandardButton.Yes, QDialog.DialogCode.Accepted, "YES DO IT", True),
        ):
            with self.subTest(first=first, second=second, text=text), TemporaryDirectory() as directory:
                with patch.object(FanControlWindow, "_load_initial_state"), patch.object(
                    FanControlWindow, "_settings_path", return_value=Path(directory) / "settings.json"
                ):
                    backend = Mock()
                    window = FanControlWindow(backend=backend)
                    window.optimus_window.show_state({"available": True, "write_available": True, "mode": "hybrid"})
                    window.optimus_window.options["dgpu"].setChecked(True)
                    window._run = Mock(side_effect=lambda operation, *args, **kwargs: operation())
                    confirmation = Mock()
                    confirmation.exec.return_value = second
                    confirmation.confirmation_text.text.return_value = text
                    try:
                        with patch("frontend.fan_control_gui.QMessageBox.question", return_value=first) as question, patch(
                            "frontend.fan_control_gui.ExperimentalMuxConfirmation", return_value=confirmation
                        ) as final_dialog:
                            window.apply_optimus()
                        if expected:
                            backend.request.assert_called_once_with("set_mux", mode="dgpu", confirmed=True,
                                                                    confirmation_text="YES DO IT")
                        else:
                            backend.request.assert_not_called()
                        self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.Cancel)
                        if first == QMessageBox.StandardButton.Cancel:
                            final_dialog.assert_not_called()
                    finally:
                        window._closing = True
                        window.tray_icon.hide()
                        window.deleteLater()
                        self.app.processEvents()
