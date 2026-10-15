import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.linux_polkit import (
    ACTION_ID, PASSWORD_TIMEOUT_SECONDS, PKCHECK, authorize_process, process_start_time,
)


def pkcheck_result(returncode, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


class PolkitAuthorizationTests(unittest.TestCase):
    def check(self, run, uid=1000):
        return authorize_process(4321, uid, run=run, start_time=lambda pid: 98765)

    def test_asks_pkcheck_for_the_exact_process_with_a_password_dialog(self):
        run = Mock(return_value=pkcheck_result(0))
        self.check(run)
        run.assert_called_once_with(
            [PKCHECK, "--action-id", ACTION_ID, "--process", "4321,98765,1000", "--allow-user-interaction"],
            capture_output=True, text=True, timeout=PASSWORD_TIMEOUT_SECONDS, check=False,
        )

    def test_root_clients_are_not_asked(self):
        run = Mock()
        self.check(run, uid=0)
        run.assert_not_called()

    def test_every_refusal_blocks_the_write_with_a_clear_reason(self):
        for returncode, stderr, reason in (
            (1, "", "denied"),
            (2, "", "desktop session"),
            (3, "", "cancelled"),
            (127, "Action io.github.salihmarangoz.stellarisfancontrol.set-gpu-mux is not registered",
             "rerun the installer"),
            (127, "something else", "something else"),
        ):
            with self.subTest(returncode=returncode, stderr=stderr):
                with self.assertRaisesRegex(PermissionError, f"{reason}.*no MUX write"):
                    self.check(Mock(return_value=pkcheck_result(returncode, stderr)))

    def test_timeout_and_missing_pkcheck_block_the_write(self):
        for error, reason in ((subprocess.TimeoutExpired(PKCHECK, 1), "in time"),
                              (FileNotFoundError(PKCHECK), "Cannot run pkcheck")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(PermissionError, f"{reason}.*no MUX write"):
                    self.check(Mock(side_effect=error))

    def test_start_time_survives_spaces_and_parentheses_in_the_command_name(self):
        fields = " ".join(str(value) for value in range(3, 22))  # fields 3..21
        stat = f"4321 (odd (name) here) {fields} 98765 23 24\n"
        with patch("backend.linux_polkit.Path.read_text", return_value=stat):
            self.assertEqual(process_start_time(4321), 98765)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_start_time_of_this_process_is_readable(self):
        self.assertGreater(process_start_time(os.getpid()), 0)


if __name__ == "__main__":
    unittest.main()
