"""Experimental MUX selection through the OEM's one UEFI display-mode byte."""

import ctypes
import hashlib
import json
import os
import struct
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


VARIABLE_NAME = "UniWillVariable"
VARIABLE_GUID = "9f33f85c-13ca-4fd1-9c4a-96217722c593"
VARIABLE_SIZE = 180
VARIABLE_ATTRIBUTES = 7
DISPLAY_MODE_OFFSET = 0x62
EXPECTED_SSID = 0x11141D05
EXPECTED_PROJECT_ID = 0x10
MODE_VALUES = {"hybrid": 0, "dgpu": 1}
EXPECTED_DSDT = "b35bf53709db9ac190ce13489619ffcf7793b78b65c7888d76e25ffc49eaea62"
EFIVAR_PATH = Path("/sys/firmware/efi/efivars") / f"{VARIABLE_NAME}-{VARIABLE_GUID}"


class FirmwareTransport(Protocol):
    def read(self) -> tuple[int, bytes]: ...
    def write(self, attributes: int, data: bytes) -> None: ...


def validate_platform() -> None:
    if os.name != "nt":
        from backend.linux_ec import validate_platform as validate_linux
        validate_linux()
        bios = Path("/sys/class/dmi/id/bios_version").read_text(encoding="ascii").strip()
        if bios != "N.1.61A15":
            raise RuntimeError("MUX control requires the validated BIOS N.1.61A15")
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
        identity = tuple(winreg.QueryValueEx(key, name)[0] for name in
                         ("BaseBoardProduct", "SystemSKU", "BIOSVersion"))
    if identity != ("GMxZGxx", "STELLARIS1XA03", "N.1.61A15"):
        raise RuntimeError("MUX control requires the validated Stellaris board, SKU, and BIOS")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    read_table = kernel.GetSystemFirmwareTable
    read_table.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
    read_table.restype = ctypes.c_uint32
    size = read_table(0x41435049, 0x54445344, None, 0)
    if not 36 <= size <= 1024 * 1024:
        raise RuntimeError("Cannot validate the firmware DSDT for MUX control")
    buffer = ctypes.create_string_buffer(size)
    if read_table(0x41435049, 0x54445344, buffer, size) != size:
        raise RuntimeError("Cannot read the complete firmware DSDT")
    if hashlib.sha256(buffer.raw).hexdigest() != EXPECTED_DSDT:
        raise RuntimeError("MUX control is disabled on an unvalidated firmware DSDT")


def conflicting_writer() -> bool:
    if os.name == "nt":
        from backend.direct_fan_control import oem_broker_available
        return oem_broker_available()
    from backend.linux_ec import tuxedo_daemon_running
    return tuxedo_daemon_running()


class LinuxFirmwareTransport:
    def __init__(self, path: Path = EFIVAR_PATH) -> None:
        self.path = path

    def read(self) -> tuple[int, bytes]:
        raw = self.path.read_bytes()
        if len(raw) != VARIABLE_SIZE + 4:
            raise RuntimeError("Unexpected UniWillVariable size; MUX control is disabled")
        return struct.unpack_from("<I", raw)[0], raw[4:]

    def write(self, attributes: int, data: bytes) -> None:
        # efivarfs expects attributes before the complete variable value. Do not
        # remove an immutable flag, recreate/delete the variable, or retry writes.
        if attributes != VARIABLE_ATTRIBUTES or len(data) != VARIABLE_SIZE:
            raise ValueError("Refusing an invalid MUX variable write")
        payload = struct.pack("<I", attributes) + data
        try:
            with self.path.open("r+b", buffering=0) as stream:
                if stream.write(payload) != len(payload):
                    raise OSError("Incomplete efivarfs write")
        except PermissionError as exc:
            raise RuntimeError(
                "Cannot write the MUX firmware variable. Root access is required; "
                "efivarfs may be read-only or the variable may be immutable. "
                "No firmware protection was removed."
            ) from exc


class WindowsFirmwareTransport:
    def __init__(self) -> None:
        from ctypes import wintypes as w
        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.GetFirmwareEnvironmentVariableExW.argtypes = [
            w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)
        ]
        self.kernel.GetFirmwareEnvironmentVariableExW.restype = w.DWORD
        self.kernel.SetFirmwareEnvironmentVariableExW.argtypes = [
            w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD
        ]
        self.kernel.SetFirmwareEnvironmentVariableExW.restype = w.BOOL

    @contextmanager
    def _privilege(self):
        w = self.w
        class Luid(ctypes.Structure):
            _fields_ = [("low", w.DWORD), ("high", w.LONG)]
        class Privilege(ctypes.Structure):
            _fields_ = [("count", w.DWORD), ("luid", Luid), ("attributes", w.DWORD)]
        self.advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        self.advapi.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(Luid)]
        self.advapi.AdjustTokenPrivileges.argtypes = [
            w.HANDLE, w.BOOL, ctypes.POINTER(Privilege), w.DWORD,
            ctypes.POINTER(Privilege), ctypes.POINTER(w.DWORD)
        ]
        token = w.HANDLE()
        if not self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x28, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        previous = Privilege()
        changed = False
        try:
            requested = Privilege()
            requested.count, requested.attributes = 1, 2
            if not self.advapi.LookupPrivilegeValueW(None, "SeSystemEnvironmentPrivilege", ctypes.byref(requested.luid)):
                raise ctypes.WinError(ctypes.get_last_error())
            length = w.DWORD()
            ctypes.set_last_error(0)
            success = self.advapi.AdjustTokenPrivileges(
                token, False, ctypes.byref(requested), ctypes.sizeof(previous),
                ctypes.byref(previous), ctypes.byref(length)
            )
            if not success or ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            changed = True
            yield
        finally:
            if changed:
                self.advapi.AdjustTokenPrivileges(token, False, ctypes.byref(previous), 0, None, None)
            self.kernel.CloseHandle(token)

    def read(self) -> tuple[int, bytes]:
        with self._privilege():
            buffer = ctypes.create_string_buffer(VARIABLE_SIZE)
            attributes = self.w.DWORD()
            count = self.kernel.GetFirmwareEnvironmentVariableExW(
                VARIABLE_NAME, "{" + VARIABLE_GUID + "}", buffer, len(buffer), ctypes.byref(attributes)
            )
            if not count:
                raise ctypes.WinError(ctypes.get_last_error())
            return attributes.value, buffer.raw[:count]

    def write(self, attributes: int, data: bytes) -> None:
        if attributes != VARIABLE_ATTRIBUTES or len(data) != VARIABLE_SIZE:
            raise ValueError("Refusing an invalid MUX variable write")
        with self._privilege():
            buffer = ctypes.create_string_buffer(data, len(data))
            if not self.kernel.SetFirmwareEnvironmentVariableExW(
                VARIABLE_NAME, "{" + VARIABLE_GUID + "}", buffer, len(data), attributes
            ):
                raise ctypes.WinError(ctypes.get_last_error())


def backup_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StellarisFanControl" / "mux-backups"
    return Path("/var/lib/stellaris-fan-control/mux-backups")


class FirmwareMux:
    """One explicit, confirmed firmware update. No startup/periodic MUX writes."""

    def __init__(
        self, *, transport: FirmwareTransport | None = None,
        platform_check: Callable[[], None] = validate_platform,
        conflict_check: Callable[[], bool] = conflicting_writer,
        backups: Path | None = None,
    ) -> None:
        self._transport = transport
        self._platform_check = platform_check
        self._conflict_check = conflict_check
        self._backups = backups
        self._lock = threading.RLock()
        self._changed = False

    def _io(self) -> FirmwareTransport:
        if self._transport is None:
            self._transport = WindowsFirmwareTransport() if os.name == "nt" else LinuxFirmwareTransport()
        return self._transport

    def _validated_read(self) -> tuple[int, bytes]:
        self._platform_check()
        attributes, data = self._io().read()
        if attributes != VARIABLE_ATTRIBUTES or len(data) != VARIABLE_SIZE:
            raise RuntimeError("Unvalidated UniWillVariable attributes or size; no MUX write allowed")
        if struct.unpack_from("<I", data)[0] != EXPECTED_SSID or data[6] != EXPECTED_PROJECT_ID:
            raise RuntimeError("UniWillVariable does not match the validated AMD Stellaris platform")
        if data[DISPLAY_MODE_OFFSET] not in MODE_VALUES.values():
            raise RuntimeError("Unknown/unsupported OemDisplayMode; MUX control is disabled")
        return attributes, data

    def _refuse_writer(self) -> None:
        if self._conflict_check():
            raise RuntimeError("Stop OEM Control Center/GCUBridge or tccd before changing NVIDIA Optimus")

    def read_state(self) -> dict[str, Any]:
        with self._lock:
            try:
                _, data = self._validated_read()
                conflict = self._conflict_check()
                return {
                    "available": True,
                    "write_available": not conflict,
                    "mode": next(mode for mode, value in MODE_VALUES.items() if value == data[DISPLAY_MODE_OFFSET]),
                    "reboot_required": True if self._changed else None,
                    "experimental": True,
                    "error": "Stop OEM Control Center/GCUBridge or tccd before changing this setting" if conflict else None,
                }
            except Exception as exc:
                return {"available": False, "write_available": False, "mode": None,
                        "experimental": True, "error": str(exc)}

    def _backup(self, attributes: int, data: bytes) -> Path:
        directory = self._backups if self._backups is not None else backup_directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"UniWillVariable-{stamp}.json"
        record = {"variable": VARIABLE_NAME, "guid": VARIABLE_GUID, "attributes": attributes,
                  "data_hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest()}
        with path.open("x", encoding="ascii") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def set_mode(self, mode: str, *, confirmed: bool, confirmation_text: str) -> dict[str, Any]:
        if type(mode) is not str or mode not in MODE_VALUES:
            raise ValueError("Select hybrid or dgpu mode")
        if confirmed is not True or type(confirmation_text) is not str or confirmation_text != "YES DO IT":
            raise PermissionError("Both MUX confirmations are required")
        with self._lock:
            self._refuse_writer()
            attributes, before = self._validated_read()
            if before[DISPLAY_MODE_OFFSET] == MODE_VALUES[mode]:
                return {**self.read_state(), "changed": False}
            backup = self._backup(attributes, before)
            self._refuse_writer()
            if self._validated_read() != (attributes, before):
                raise RuntimeError("Firmware changed concurrently; no MUX write was performed")
            desired = bytearray(before)
            desired[DISPLAY_MODE_OFFSET] = MODE_VALUES[mode]
            self._refuse_writer()
            self._io().write(attributes, bytes(desired))
            if self._validated_read() != (attributes, bytes(desired)):
                # Do not blindly restore an entire firmware blob after a failed
                # verification: it could overwrite a concurrent firmware change.
                raise RuntimeError(f"MUX firmware readback failed. No retry or reboot was performed. Backup: {backup}")
            self._changed = True
            return {**self.read_state(), "changed": True, "backup": str(backup)}
