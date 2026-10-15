"""Ask polkit for an administrator password before a Linux firmware MUX write."""

import subprocess
from pathlib import Path
from typing import Any, Callable

ACTION_ID = "io.github.salihmarangoz.stellarisfancontrol.set-gpu-mux"
PKCHECK = "/usr/bin/pkcheck"
# The GUI waits up to 180 s for set_mux; stop waiting for the password before that.
PASSWORD_TIMEOUT_SECONDS = 150.0

PKCHECK_ERRORS = {
    1: "Authentication failed or was denied",
    2: "No polkit password dialog is available; open the application in your desktop session",
    3: "The password dialog was cancelled",
}


def process_start_time(pid: int) -> int:
    # Field 22 of /proc/<pid>/stat; the command name in field 2 may contain spaces.
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    return int(stat.rsplit(")", 1)[1].split()[19])


def authorize_process(
    pid: int,
    uid: int,
    *,
    run: Callable[..., Any] = subprocess.run,
    start_time: Callable[[int], int] = process_start_time,
) -> None:
    """Return only when polkit authorized the requesting process; raise otherwise."""
    if uid == 0:
        return  # root already has every power this check could grant
    # pid,start-time,uid lets polkit reject a reused PID.
    subject = f"{pid},{start_time(pid)},{uid}"
    try:
        result = run(
            [PKCHECK, "--action-id", ACTION_ID, "--process", subject, "--allow-user-interaction"],
            capture_output=True, text=True, timeout=PASSWORD_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        raise PermissionError("The password was not entered in time; no MUX write was performed") from None
    except OSError as exc:
        raise PermissionError(f"Cannot run pkcheck ({exc}); no MUX write was performed") from None
    if result.returncode != 0:
        if "not registered" in result.stderr:
            reason = "The polkit rule for MUX changes is not installed; rerun the installer"
        else:
            reason = PKCHECK_ERRORS.get(result.returncode, f"polkit check failed: {result.stderr.strip()}")
        raise PermissionError(f"{reason}; no MUX write was performed")
