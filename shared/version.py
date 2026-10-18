"""Application version shown in the GUI header.

APP_VERSION is the release this code belongs to. Bump it before tagging; the
Release workflow refuses a tag that does not match. Installs from a git
checkout show the exact build instead, so builds between releases can be told
apart.
"""

import os
import subprocess
from pathlib import Path

APP_VERSION = "1.3.1"
# Written by scripts/linux/install.sh from `git describe` when installing from a checkout.
BUILD_VERSION_FILENAME = "build-version.txt"
APP_ROOT = Path(__file__).resolve().parents[1]


def display_version(root: Path = APP_ROOT) -> str:
    try:
        recorded = (root / BUILD_VERSION_FILENAME).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        recorded = ""
    if recorded:
        return recorded
    if (root / ".git").exists():
        try:
            described = subprocess.run(
                ["git", "-C", str(root), "describe", "--tags", "--dirty", "--always"],
                capture_output=True, text=True, timeout=2, check=True,
                # A source-mode GUI on Windows must not flash a console window.
                creationflags=0x08000000 if os.name == "nt" else 0,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            described = ""
        if described:
            return described
    return f"v{APP_VERSION}"
