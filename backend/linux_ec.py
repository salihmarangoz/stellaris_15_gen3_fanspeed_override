import hashlib
import mmap
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


EXPECTED_BOARD_NAME = "GMxZGxx"
EXPECTED_PRODUCT_SKU = "STELLARIS1XA03"
# DSDT of BIOS N.1.61A15, validated on the target Stellaris 15 Gen3. Its INOU
# ECRR/ECRW methods access EC RAM as single bytes at EC_WINDOW_BASE + address.
EXPECTED_DSDT_SHA256 = "b35bf53709db9ac190ce13489619ffcf7793b78b65c7888d76e25ffc49eaea62"
DMI_DIRECTORY = Path("/sys/class/dmi/id")
DSDT_PATH = Path("/sys/firmware/acpi/tables/DSDT")
PROC_DIRECTORY = Path("/proc")
EC_WINDOW_BASE = 0xFE200000
EC_WINDOW_SIZE = 0x1000
# The OEM software and the mainline uniwill-laptop driver wait 6 ms per access.
EC_ACCESS_DELAY_SECONDS = 0.006

PROJECT_ID_ADDRESS = 0x0740
EXPECTED_PROJECT_ID = 0x10
AP_OEM_ADDRESS = 0x0741
ENABLE_MANUAL_CONTROL = 0x01
FAN_ABNORMAL = 0x20
CTGP_CONTROL_ADDRESS = 0x0743
CTGP_GENERAL_ENABLE = 0x01
CTGP_DYNAMIC_BOOST_ENABLE = 0x02
CTGP_OFFSET_ENABLE = 0x04
CTGP_OFFSET_ADDRESS = 0x0744
CTGP_TPP_OFFSET_ADDRESS = 0x0745
CTGP_DB_OFFSET_ADDRESS = 0x0746
# Values the mainline and TUXEDO drivers program when they enable cTGP.
CTGP_TPP_OFFSET = 0xFF
CTGP_DB_OFFSET = 25
MAX_CTGP_OFFSET_WATTS = 50
FAN_MODE_ADDRESS = 0x0751
FAN_MODE_USER_HIGH = 0xA0
FAN_MODE_BOOST = 0x40
KNOWN_FAN_MODES = {0x00, 0x10, 0x40, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0xA0}
CPU_PWM_ADDRESS = 0x075B
GPU_PWM_ADDRESS = 0x075C
CPU_FAN_RPM_ADDRESS = 0x0464
GPU_FAN_RPM_ADDRESS = 0x046C
MAX_PLAUSIBLE_RPM = 10000
FAN_CAPABILITY_ADDRESS = 0x078E
UNIVERSAL_FAN_CONTROL_SUPPORTED = 0x40
TABLE_CONTROL_ADDRESS = 0x07C5
SPLIT_TABLES = 0x80
AP_OEM_6_ADDRESS = 0x07C6
ENABLE_FAN_TABLES = 0x04

TABLE_POINT_COUNT = 16
CPU_TEMP_END_ADDRESS = 0x0F00
CPU_TEMP_START_ADDRESS = 0x0F10
CPU_DUTY_ADDRESS = 0x0F20
GPU_TEMP_END_ADDRESS = 0x0F30
GPU_TEMP_START_ADDRESS = 0x0F40
GPU_DUTY_ADDRESS = 0x0F50
# Zone thresholds TUXEDO programs on this laptop. Every zone receives the same
# duty, so the unreliable EC temperature cannot change the fan speed.
TABLE_END_TEMPERATURES = (115,) + tuple(116 + index for index in range(1, 16))
TABLE_START_TEMPERATURES = (0,) + tuple(115 + index for index in range(1, 16))
# A raw duty of 0 makes the EC spin the fan at 30% for three minutes first.
FAN_OFF_RAW_DUTY = 1
MAX_RAW_DUTY = 200

CONTROL_ADDRESSES = (AP_OEM_ADDRESS, FAN_MODE_ADDRESS, TABLE_CONTROL_ADDRESS, AP_OEM_6_ADDRESS)
TABLE_ADDRESSES = tuple(
    base + offset
    for offset in range(TABLE_POINT_COUNT)
    for base in (
        CPU_TEMP_END_ADDRESS,
        CPU_TEMP_START_ADDRESS,
        CPU_DUTY_ADDRESS,
        GPU_TEMP_END_ADDRESS,
        GPU_TEMP_START_ADDRESS,
        GPU_DUTY_ADDRESS,
    )
)
MANAGED_ADDRESSES = frozenset(CONTROL_ADDRESSES + TABLE_ADDRESSES)
# Tables first, enable bits last: the order validated live on the target laptop.
WRITE_ORDER = TABLE_ADDRESSES + CONTROL_ADDRESSES
RESTORE_ORDER = TABLE_ADDRESSES + (
    FAN_MODE_ADDRESS,
    TABLE_CONTROL_ADDRESS,
    AP_OEM_6_ADDRESS,
    AP_OEM_ADDRESS,
)
CTGP_ADDRESSES = (
    CTGP_OFFSET_ADDRESS,
    CTGP_TPP_OFFSET_ADDRESS,
    CTGP_DB_OFFSET_ADDRESS,
    CTGP_CONTROL_ADDRESS,
)


class EcTransport(Protocol):
    def read(self, address: int) -> int: ...

    def write(self, address: int, value: int) -> None: ...

    def close(self) -> None: ...


class DevMemEcWindow:
    """Byte access to the firmware's EC RAM window, exactly as INOU ECRR/ECRW do."""

    def __init__(self, *, writable: bool) -> None:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_SYNC
        protection = mmap.PROT_READ | (mmap.PROT_WRITE if writable else 0)
        try:
            descriptor = os.open("/dev/mem", flags)
        except OSError as exc:
            raise RuntimeError("Cannot open /dev/mem; direct EC control requires root") from exc
        try:
            self._map = mmap.mmap(
                descriptor,
                EC_WINDOW_SIZE,
                mmap.MAP_SHARED,
                protection,
                offset=EC_WINDOW_BASE,
            )
        except OSError as exc:
            raise RuntimeError("Cannot map the EC RAM window") from exc
        finally:
            os.close(descriptor)

    def read(self, address: int) -> int:
        if not 0 <= address < EC_WINDOW_SIZE:
            raise ValueError(f"EC address 0x{address:04X} is outside the window")
        value = self._map[address]
        time.sleep(EC_ACCESS_DELAY_SECONDS)
        return value

    def write(self, address: int, value: int) -> None:
        if not 0 <= address < EC_WINDOW_SIZE:
            raise ValueError(f"EC address 0x{address:04X} is outside the window")
        self._map[address] = value
        time.sleep(EC_ACCESS_DELAY_SECONDS)

    def close(self) -> None:
        self._map.close()


def validate_platform(
    dmi_directory: Path = DMI_DIRECTORY, dsdt_path: Path = DSDT_PATH
) -> None:
    try:
        board = (dmi_directory / "board_name").read_text(encoding="ascii").strip()
        sku = (dmi_directory / "product_sku").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeError("Cannot identify the laptop from DMI") from exc
    if (board, sku) != (EXPECTED_BOARD_NAME, EXPECTED_PRODUCT_SKU):
        raise RuntimeError(
            f"Unsupported laptop {board}/{sku}; direct EC control is disabled"
        )
    try:
        digest = hashlib.sha256(dsdt_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("Cannot read the ACPI DSDT; direct EC control requires root") from exc
    if digest != EXPECTED_DSDT_SHA256:
        raise RuntimeError(
            "The firmware DSDT is not the validated BIOS N.1.61A15 build; "
            "direct EC control is disabled"
        )


def tuxedo_daemon_running(proc_directory: Path = PROC_DIRECTORY) -> bool:
    for entry in proc_directory.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if os.path.basename(command.split(b"\0", 1)[0]) == b"tccd":
            return True
    return False


def encode_duty(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Fan duty must be an integer percentage")
    duty = int(value)
    if duty != value or not 0 <= duty <= 100:
        raise ValueError("Fan duty must be an integer from 0 to 100")
    return FAN_OFF_RAW_DUTY if duty == 0 else duty * 2


def decode_duty(raw: int) -> int:
    if raw == FAN_OFF_RAW_DUTY:
        return 0
    return min(100, raw // 2)


class LinuxEcClient:
    """Validated fan-table and cTGP access for the target laptop on Linux."""

    method_name = "direct_ec"

    def __init__(
        self,
        *,
        transport_factory: Callable[[], EcTransport] | None = None,
        platform_check: Callable[[], None] = validate_platform,
        conflict_check: Callable[[], bool] = tuxedo_daemon_running,
    ) -> None:
        self._transport_factory = transport_factory or (
            lambda: DevMemEcWindow(writable=True)
        )
        self._platform_check = platform_check
        self._conflict_check = conflict_check
        self._transport: EcTransport | None = None
        self._lock = threading.RLock()

    def connect(self) -> None:
        with self._lock:
            if self._transport is not None:
                return
            self._platform_check()
            self._transport = self._transport_factory()
            try:
                self._validate_hardware()
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self._transport is not None:
                self._transport.close()
                self._transport = None

    def _read(self, address: int) -> int:
        if self._transport is None:
            raise RuntimeError("Direct EC client is not connected")
        value = int(self._transport.read(address))
        if not 0 <= value <= 0xFF:
            raise RuntimeError(f"Invalid EC byte at 0x{address:04X}: {value}")
        return value

    def _write(self, address: int, value: int) -> None:
        if self._transport is None:
            raise RuntimeError("Direct EC client is not connected")
        if not 0 <= value <= 0xFF:
            raise ValueError(f"Invalid EC byte for 0x{address:04X}: {value}")
        self._transport.write(address, value)

    def _validate_hardware(self) -> None:
        project_id = self._read(PROJECT_ID_ADDRESS)
        if project_id != EXPECTED_PROJECT_ID:
            raise RuntimeError(
                f"Unexpected EC project ID 0x{project_id:02X}; direct fan control is disabled"
            )
        if not self._read(FAN_CAPABILITY_ADDRESS) & UNIVERSAL_FAN_CONTROL_SUPPORTED:
            raise RuntimeError("The EC does not report universal fan-table control")
        mode = self._read(FAN_MODE_ADDRESS)
        if mode not in KNOWN_FAN_MODES:
            raise RuntimeError(
                f"Unexpected fan-mode byte 0x{mode:02X}; direct fan control is disabled"
            )

    def refuse_conflicting_writer(self) -> None:
        if self._conflict_check():
            raise RuntimeError(
                "The TUXEDO Control Center daemon (tccd) is running; stop or "
                "uninstall it before using direct EC control"
            )

    def read_registers(self, addresses: tuple[int, ...]) -> dict[int, int]:
        return {address: self._read(address) for address in addresses}

    def _apply(self, desired: dict[int, int], order: tuple[int, ...], restore_order: tuple[int, ...]) -> bool:
        with self._lock:
            self.refuse_conflicting_writer()
            previous = self.read_registers(tuple(address for address in order if address in desired))
            changed = [
                address
                for address in order
                if address in desired and previous[address] != desired[address]
            ]
            if not changed:
                return False
            try:
                for address in changed:
                    self._write(address, desired[address])
                mismatched = [
                    address
                    for address in order
                    if address in desired and self._read(address) != desired[address]
                ]
                if mismatched:
                    raise RuntimeError(
                        "EC readback did not match the direct write at "
                        + ", ".join(f"0x{address:04X}" for address in mismatched)
                    )
            except Exception:
                for address in restore_order:
                    if address in changed:
                        self._write(address, previous[address])
                raise
            return True

    def _desired_state(self, cpu_raw: int | None, gpu_raw: int | None, boost: bool) -> dict[int, int]:
        control = self.read_registers((AP_OEM_ADDRESS, TABLE_CONTROL_ADDRESS, AP_OEM_6_ADDRESS))
        desired: dict[int, int] = {}
        if cpu_raw is not None and gpu_raw is not None:
            for offset in range(TABLE_POINT_COUNT):
                desired[CPU_TEMP_END_ADDRESS + offset] = TABLE_END_TEMPERATURES[offset]
                desired[CPU_TEMP_START_ADDRESS + offset] = TABLE_START_TEMPERATURES[offset]
                desired[CPU_DUTY_ADDRESS + offset] = cpu_raw
                desired[GPU_TEMP_END_ADDRESS + offset] = TABLE_END_TEMPERATURES[offset]
                desired[GPU_TEMP_START_ADDRESS + offset] = TABLE_START_TEMPERATURES[offset]
                desired[GPU_DUTY_ADDRESS + offset] = gpu_raw
        desired[AP_OEM_ADDRESS] = control[AP_OEM_ADDRESS] | ENABLE_MANUAL_CONTROL
        desired[FAN_MODE_ADDRESS] = FAN_MODE_BOOST if boost else FAN_MODE_USER_HIGH
        desired[TABLE_CONTROL_ADDRESS] = control[TABLE_CONTROL_ADDRESS] | SPLIT_TABLES
        desired[AP_OEM_6_ADDRESS] = control[AP_OEM_6_ADDRESS] | ENABLE_FAN_TABLES
        return desired

    def apply_duties(self, cpu: int, gpu: int) -> bool:
        """Write one duty per fan to all 16 zones, leave Boost, and verify.

        Returns False when the EC already held exactly this state.
        """
        cpu_raw = encode_duty(cpu)
        gpu_raw = encode_duty(gpu)
        with self._lock:
            desired = self._desired_state(cpu_raw, gpu_raw, boost=False)
            return self._apply(desired, WRITE_ORDER, RESTORE_ORDER)

    def set_boost(self, enabled: bool) -> bool:
        if not enabled:
            raise ValueError("Leave Boost by applying fan duties")
        with self._lock:
            desired = self._desired_state(None, None, boost=True)
            return self._apply(desired, WRITE_ORDER, RESTORE_ORDER)

    def boost_enabled(self) -> bool:
        return self._read(FAN_MODE_ADDRESS) == FAN_MODE_BOOST

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            registers = self.read_registers(tuple(sorted(MANAGED_ADDRESSES)))
        return {
            "Name": "LINUX_EC",
            "Platform": {
                "board_name": EXPECTED_BOARD_NAME,
                "product_sku": EXPECTED_PRODUCT_SKU,
                "dsdt_sha256": EXPECTED_DSDT_SHA256,
            },
            "Registers": {f"0x{address:04X}": value for address, value in registers.items()},
        }

    def restore(self, snapshot: dict[str, Any]) -> bool:
        if snapshot.get("Name") != "LINUX_EC" or not isinstance(snapshot.get("Registers"), dict):
            raise ValueError("This is not a Linux direct-EC backup")
        desired: dict[int, int] = {}
        for key, value in snapshot["Registers"].items():
            address = int(str(key), 16)
            if address not in MANAGED_ADDRESSES:
                raise ValueError(f"The backup contains an unmanaged address {key}")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
                raise ValueError(f"The backup contains an invalid byte for {key}")
            desired[address] = value
        if set(desired) != MANAGED_ADDRESSES:
            raise ValueError("The backup does not contain every managed register")
        if desired[FAN_MODE_ADDRESS] not in KNOWN_FAN_MODES:
            raise ValueError("The backup has an unknown fan-mode byte")
        return self._apply(desired, RESTORE_ORDER, RESTORE_ORDER)

    def curve(self) -> dict[str, Any]:
        with self._lock:
            cpu = [self._read(CPU_DUTY_ADDRESS + offset) for offset in range(TABLE_POINT_COUNT)]
            gpu = [self._read(GPU_DUTY_ADDRESS + offset) for offset in range(TABLE_POINT_COUNT)]
        return {
            "Name": "LINUX_EC",
            "CPU": [{"ID": index, "Duty": decode_duty(raw)} for index, raw in enumerate(cpu)],
            "GPU": [{"ID": index, "Duty": decode_duty(raw)} for index, raw in enumerate(gpu)],
        }

    def _read_rpm(self, address: int) -> int | None:
        rpm = (self._read(address) << 8) | self._read(address + 1)
        return rpm if rpm <= MAX_PLAUSIBLE_RPM else None

    def fan_info(self) -> dict[str, Any]:
        with self._lock:
            cpu = self._read(CPU_PWM_ADDRESS)
            gpu = self._read(GPU_PWM_ADDRESS)
            if cpu > MAX_RAW_DUTY or gpu > MAX_RAW_DUTY:
                raise RuntimeError("The EC reported an implausible live fan duty")
            return {
                # Live ramp values use half-percent units and can be odd.
                "CpuFanDuty": cpu / 2,
                "GpuFanDuty": gpu / 2,
                "CpuFanRpm": self._read_rpm(CPU_FAN_RPM_ADDRESS),
                "GpuFanRpm": self._read_rpm(GPU_FAN_RPM_ADDRESS),
                "FanAbnormal": bool(self._read(AP_OEM_ADDRESS) & FAN_ABNORMAL),
                "ControlMethod": self.method_name,
            }

    def read_ctgp_offset(self) -> int:
        return self._read(CTGP_OFFSET_ADDRESS)

    def read_ctgp_state(self) -> dict[str, Any]:
        with self._lock:
            control = self._read(CTGP_CONTROL_ADDRESS)
            return {
                "offset": self._read(CTGP_OFFSET_ADDRESS),
                "dynamic_boost": bool(
                    control & CTGP_GENERAL_ENABLE and control & CTGP_DYNAMIC_BOOST_ENABLE
                ),
                "dynamic_boost_w": self._read(CTGP_DB_OFFSET_ADDRESS),
            }

    def dynamic_boost_enabled(self) -> bool:
        return bool(self.read_ctgp_state()["dynamic_boost"])

    def write_ctgp_offset(self, watts: int, *, dynamic_boost: bool | None = None) -> bool:
        """Program cTGP directly, as the kernel drivers do, when no driver exposes it.

        dynamic_boost=None enables Dynamic Boost like the drivers' initialization.
        """
        if isinstance(watts, bool) or not 0 <= int(watts) <= MAX_CTGP_OFFSET_WATTS:
            raise ValueError(f"The cTGP offset must be from 0 to {MAX_CTGP_OFFSET_WATTS} W")
        with self._lock:
            control = self._read(CTGP_CONTROL_ADDRESS) | CTGP_GENERAL_ENABLE | CTGP_OFFSET_ENABLE
            if dynamic_boost is False:
                control &= ~CTGP_DYNAMIC_BOOST_ENABLE
            else:
                control |= CTGP_DYNAMIC_BOOST_ENABLE
            desired = {
                CTGP_OFFSET_ADDRESS: int(watts),
                CTGP_TPP_OFFSET_ADDRESS: CTGP_TPP_OFFSET,
                CTGP_DB_OFFSET_ADDRESS: CTGP_DB_OFFSET,
                CTGP_CONTROL_ADDRESS: control,
            }
            return self._apply(desired, CTGP_ADDRESSES, CTGP_ADDRESSES)

    def write_dynamic_boost(self, enabled: bool) -> bool:
        """Toggle NVIDIA Dynamic Boost the way TUXEDO's db_enable attribute does."""
        with self._lock:
            control = self._read(CTGP_CONTROL_ADDRESS)
            if enabled:
                desired = {
                    CTGP_DB_OFFSET_ADDRESS: CTGP_DB_OFFSET,
                    CTGP_CONTROL_ADDRESS: control
                    | CTGP_GENERAL_ENABLE
                    | CTGP_DYNAMIC_BOOST_ENABLE,
                }
            else:
                control &= ~CTGP_DYNAMIC_BOOST_ENABLE
                if not control & CTGP_OFFSET_ENABLE:
                    control &= ~CTGP_GENERAL_ENABLE
                desired = {CTGP_CONTROL_ADDRESS: control}
            return self._apply(desired, CTGP_ADDRESSES, CTGP_ADDRESSES)
