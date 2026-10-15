import json
import os
import struct
import unittest
from contextlib import nullcontext
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
        # Most tests check the firmware flow itself; the password tests pass an authorizer.
        self.mux = FirmwareMux(transport=self.io, platform_check=self.platform,
                               conflict_check=self.conflict, backups=Path(self.directory.name),
                               require_authorization=False)

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

    def test_password_is_asked_once_before_any_backup_or_write(self):
        seen = []
        authorize = Mock(side_effect=lambda: seen.append(
            (len(self.io.writes), len(list(Path(self.directory.name).iterdir())))))
        self.assertTrue(self.save(authorize=authorize)["changed"])
        authorize.assert_called_once_with()
        self.assertEqual(seen, [(0, 0)])
        self.assertEqual(len(self.io.writes), 1)

    def test_no_password_prompt_when_the_mode_is_already_saved(self):
        authorize = Mock()
        self.assertFalse(self.save("hybrid", authorize=authorize)["changed"])
        authorize.assert_not_called()
        self.assertEqual(self.io.writes, [])

    def test_denied_password_prevents_backup_and_write(self):
        authorize = Mock(side_effect=PermissionError("The password dialog was cancelled"))
        with self.assertRaisesRegex(PermissionError, "cancelled"):
            self.save(authorize=authorize)
        self.assertEqual(self.io.writes, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_linux_refuses_a_save_without_a_password_check(self):
        with patch("backend.gpu_mux.os.name", "posix"):
            mux = FirmwareMux(transport=self.io, platform_check=self.platform,
                              conflict_check=self.conflict, backups=Path(self.directory.name))
            with self.assertRaisesRegex(PermissionError, "password check is required"):
                mux.set_mode("dgpu", confirmed=True, confirmation_text="YES DO IT")
        self.assertEqual(self.io.writes, [])
        # Windows keeps its UAC-elevated flow without a password callback.
        with patch("backend.gpu_mux.os.name", "nt"):
            self.assertFalse(FirmwareMux(transport=self.io)._require_authorization)

    def test_a_change_while_waiting_for_the_password_is_not_written(self):
        def another_save():
            # Someone else writes the firmware while our password dialog is open.
            changed = bytearray(self.io.data)
            changed[10] ^= 0xFF
            self.io.data = bytes(changed)

        with self.assertRaisesRegex(RuntimeError, "while waiting for the password"):
            self.save(authorize=Mock(side_effect=another_save))
        self.assertEqual(self.io.writes, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_a_request_that_saw_no_change_cannot_write_after_another_save(self):
        # Race found in review: B's pre-check sees dGPU already saved, so it asks
        # no password; A then saves hybrid; B's locked section must not write dGPU.
        self.io.data = firmware_blob(mode=1)
        original_read, reads = self.io.read, []

        def read():
            reads.append(1)
            result = original_read()
            if len(reads) == 1:
                self.io.data = firmware_blob(mode=0)  # A's authorized save lands now
            return result

        self.io.read = read
        authorize = Mock()
        with self.assertRaisesRegex(RuntimeError, "while waiting for the password"):
            self.save("dgpu", authorize=authorize)
        authorize.assert_not_called()
        self.assertEqual(self.io.writes, [])

    def test_password_is_not_asked_without_confirmations_or_with_another_writer(self):
        authorize = Mock()
        with self.assertRaises(PermissionError):
            self.save(confirmation_text="yes do it", authorize=authorize)
        self.conflict.return_value = True
        with self.assertRaisesRegex(RuntimeError, "GCUBridge"):
            self.save(authorize=authorize)
        authorize.assert_not_called()
        self.assertEqual(self.io.writes, [])

    def linux_variable(self, flags):
        path = Path(self.directory.name) / "variable"
        path.write_bytes(struct.pack("<I", 7) + firmware_blob())
        state = {"flags": flags}
        calls = []

        def set_flags(descriptor, value):
            calls.append(value)
            state["flags"] = value

        original_open = Path.open

        def open_like_efivarfs(self_path, mode="r", *args, **kwargs):
            # efivarfs refuses to open an immutable variable for writing.
            if "+" in mode and self_path == path and state["flags"] & 0x10:
                raise PermissionError(1, "Operation not permitted", str(self_path))
            return original_open(self_path, mode, *args, **kwargs)

        patches = (patch.object(LinuxFirmwareTransport, "_get_flags", side_effect=lambda d: state["flags"]),
                   patch.object(LinuxFirmwareTransport, "_set_flags", side_effect=set_flags),
                   patch.object(Path, "open", open_like_efivarfs))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        io = LinuxFirmwareTransport(path)
        attributes, data = io.read()
        changed = bytearray(data)
        changed[DISPLAY_MODE_OFFSET] = 1
        return path, io, calls, attributes, bytes(changed)

    def test_linux_immutable_flag_is_lifted_only_for_the_write_and_restored(self):
        path, io, calls, attributes, changed = self.linux_variable(0x10 | 0x80000)
        io.write(attributes, changed)
        self.assertEqual(calls, [0x80000, 0x10 | 0x80000])
        self.assertEqual(path.read_bytes()[4 + DISPLAY_MODE_OFFSET], 1)

    def test_linux_flags_are_untouched_when_the_variable_is_not_immutable(self):
        path, io, calls, attributes, changed = self.linux_variable(0x80000)
        io.write(attributes, changed)
        self.assertEqual(calls, [])
        self.assertEqual(path.read_bytes()[4 + DISPLAY_MODE_OFFSET], 1)

    def test_linux_immutable_flag_is_restored_when_the_write_fails(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("needs POSIX file permissions that apply to this user")
        path, io, calls, attributes, changed = self.linux_variable(0x10)
        path.chmod(0o400)
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            io.write(attributes, changed)
        self.assertEqual(calls, [0, 0x10])

    def test_linux_restore_failure_is_logged_and_the_saved_value_stands(self):
        path, io, calls, attributes, changed = self.linux_variable(0x10)
        clear_flag = LinuxFirmwareTransport._set_flags.side_effect

        def clear_then_fail(descriptor, value):
            if calls:
                raise OSError("busy")
            clear_flag(descriptor, value)

        LinuxFirmwareTransport._set_flags.side_effect = clear_then_fail
        with self.assertLogs("stellaris15gen3", "WARNING") as logs:
            io.write(attributes, changed)
        self.assertIn("next boot", logs.output[0])
        self.assertEqual(path.read_bytes()[4 + DISPLAY_MODE_OFFSET], 1)

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
        # Linux exercises the real inode-flags ioctl on the temporary file; Windows CI has no fcntl.
        flags = patch.object(LinuxFirmwareTransport, "_get_flags", return_value=0) if os.name == "nt" else nullcontext()
        with flags:
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
        from frontend.fan_control_gui import MUX_SAVE_TIMEOUT_SECONDS, FanControlWindow
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
                                                                    confirmation_text="YES DO IT",
                                                                    request_timeout=MUX_SAVE_TIMEOUT_SECONDS)
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

    def save_through_gui(self, run):
        """Confirm both dialogs and hand the set_mux request to `run`; return the window state."""
        from PySide6.QtWidgets import QMessageBox, QDialog
        from frontend.fan_control_gui import FanControlWindow
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(FanControlWindow, "_load_initial_state"), patch.object(
            FanControlWindow, "_settings_path", return_value=Path(directory.name) / "settings.json"
        ):
            window = FanControlWindow(backend=Mock())
        self.addCleanup(self.app.processEvents)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.tray_icon.hide)
        self.addCleanup(setattr, window, "_closing", True)
        window.optimus_window.show_state({"available": True, "write_available": True, "mode": "hybrid"})
        window.optimus_window.options["dgpu"].setChecked(True)
        window._run = Mock(side_effect=run)
        confirmation = Mock()
        confirmation.exec.return_value = QDialog.DialogCode.Accepted
        confirmation.confirmation_text.text.return_value = "YES DO IT"
        with patch("frontend.fan_control_gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("frontend.fan_control_gui.ExperimentalMuxConfirmation", return_value=confirmation), \
                patch("frontend.fan_control_gui.QMessageBox.warning") as warning, \
                patch.object(window, "open_optimus") as reread:
            window.apply_optimus()
        return window, warning, reread

    def test_a_cancelled_password_shows_one_line_and_rereads_the_saved_mode(self):
        details = ("Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
                   "RuntimeError: The password dialog was cancelled; no MUX write was performed\n")
        window, warning, reread = self.save_through_gui(
            lambda operation, complete, message, on_failed=None, **kwargs: on_failed(details))
        reason = "The password dialog was cancelled; no MUX write was performed"
        self.assertEqual(window._status_message, "NVIDIA Optimus: save failed | " + reason)
        self.assertNotIn("Traceback", warning.call_args.args[2])
        self.assertTrue(warning.call_args.args[2].startswith(reason))
        reread.assert_called_once_with()

    def test_an_unchanged_mode_is_not_reported_as_saved(self):
        state = {"available": True, "write_available": True, "mode": "dgpu", "changed": False}
        window, warning, reread = self.save_through_gui(
            lambda operation, complete, message, on_failed=None, **kwargs: complete(state))
        self.assertIn("nothing was written", window._status_message)
        warning.assert_not_called()
