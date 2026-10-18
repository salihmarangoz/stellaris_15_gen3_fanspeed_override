import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from shared import version


class DisplayVersionTests(unittest.TestCase):
    def test_release_archive_shows_the_release_version(self):
        with TemporaryDirectory() as root:
            self.assertEqual(version.display_version(Path(root)), f"v{version.APP_VERSION}")

    def test_installer_recorded_build_wins(self):
        with TemporaryDirectory() as root:
            (Path(root) / version.BUILD_VERSION_FILENAME).write_text("v1.3.1-4-gabc1234\n")
            (Path(root) / ".git").mkdir()
            with patch("shared.version.subprocess.run") as run:
                self.assertEqual(version.display_version(Path(root)), "v1.3.1-4-gabc1234")
            run.assert_not_called()

    def test_git_checkout_shows_git_describe_and_falls_back_on_failure(self):
        with TemporaryDirectory() as root:
            (Path(root) / ".git").mkdir()
            described = subprocess.CompletedProcess([], 0, stdout="v1.3.1-2-gdeadbee-dirty\n")
            with patch("shared.version.subprocess.run", return_value=described):
                self.assertEqual(version.display_version(Path(root)), "v1.3.1-2-gdeadbee-dirty")
            with patch("shared.version.subprocess.run", side_effect=FileNotFoundError("git")):
                self.assertEqual(version.display_version(Path(root)), f"v{version.APP_VERSION}")


if __name__ == "__main__":
    unittest.main()
