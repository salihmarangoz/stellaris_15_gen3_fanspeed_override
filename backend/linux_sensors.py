import csv
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


SYSFS = Path("/sys")
NVIDIA_VENDOR_ID = "0x10de"
NVIDIA_SMI_TIMEOUT_SECONDS = 8
MAX_PLAUSIBLE_TEMPERATURE = 120.0
MAX_PLAUSIBLE_CPU_POWER_WATTS = 400.0
MIN_POWER_INTERVAL_SECONDS = 0.5
NVIDIA_QUERY_FIELDS = (
    "name",
    "temperature.gpu",
    "power.draw",
    "power.default_limit",
    "power.max_limit",
    "enforced.power.limit",
)


@dataclass(frozen=True)
class SensorReadings:
    cpu_c: float
    gpu_c: float | None
    cpu_source: str
    gpu_source: str
    gpu_powered_off: bool = False
    cpu_power_w: float | None = None
    gpu_power_w: float | None = None


@dataclass(frozen=True)
class NvidiaLimits:
    default_limit_w: float | None
    max_limit_w: float | None
    enforced_limit_w: float | None


@dataclass(frozen=True)
class GpuReading:
    temperature: float | None
    source: str
    powered_off: bool
    power_w: float | None
    limits: NvidiaLimits | None


def read_cpu_temperature(sysfs: Path = SYSFS) -> tuple[float, str]:
    """Read Ryzen Tctl from k10temp, which decodes the same SMN register as Windows."""
    hwmon_root = sysfs / "class" / "hwmon"
    try:
        entries = sorted(hwmon_root.iterdir())
    except OSError as exc:
        raise RuntimeError("Cannot list hwmon sensors") from exc
    for hwmon in entries:
        try:
            if (hwmon / "name").read_text(encoding="ascii").strip() != "k10temp":
                continue
            label = (hwmon / "temp1_label").read_text(encoding="ascii").strip()
            raw = (hwmon / "temp1_input").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if label != "Tctl":
            raise RuntimeError(f"Unexpected k10temp temp1 label {label!r}")
        try:
            celsius = int(raw) / 1000
        except ValueError as exc:
            raise RuntimeError(f"Malformed k10temp reading {raw!r}") from exc
        if not 0 < celsius <= MAX_PLAUSIBLE_TEMPERATURE:
            raise RuntimeError(f"Implausible Ryzen Tctl temperature: {celsius:.1f} C")
        return celsius, "k10temp Ryzen SMN (Tctl)"
    raise RuntimeError("The k10temp Ryzen sensor is not available")


def find_nvidia_gpu(sysfs: Path = SYSFS) -> Path | None:
    try:
        devices = sorted((sysfs / "bus" / "pci" / "devices").iterdir())
    except OSError:
        return None
    for device in devices:
        try:
            vendor = (device / "vendor").read_text(encoding="ascii").strip()
            device_class = (device / "class").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if vendor == NVIDIA_VENDOR_ID and device_class.startswith("0x03"):
            return device
    return None


def gpu_runtime_suspended(device: Path) -> bool:
    try:
        status = (device / "power" / "runtime_status").read_text(encoding="ascii").strip()
    except OSError:
        return False
    return status == "suspended"


def _optional_float(value: str) -> float | None:
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def query_nvidia(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> GpuReading:
    try:
        result = runner(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(NVIDIA_QUERY_FIELDS)}",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("nvidia-smi is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("nvidia-smi timed out") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"nvidia-smi failed: {details}") from exc
    rows = list(csv.reader(result.stdout.strip().splitlines()))
    if not rows or len(rows[0]) != len(NVIDIA_QUERY_FIELDS):
        raise RuntimeError(f"Unexpected nvidia-smi output: {result.stdout!r}")
    name, temperature, power, default_limit, max_limit, enforced_limit = rows[0]
    try:
        celsius = float(temperature.strip())
    except ValueError as exc:
        raise RuntimeError(f"Malformed NVIDIA temperature {temperature!r}") from exc
    if not 0 < celsius <= MAX_PLAUSIBLE_TEMPERATURE:
        raise RuntimeError(f"Implausible NVIDIA temperature: {celsius:.1f} C")
    limits = NvidiaLimits(
        _optional_float(default_limit),
        _optional_float(max_limit),
        _optional_float(enforced_limit),
    )
    return GpuReading(
        celsius, f"NVIDIA driver ({name.strip()})", False, _optional_float(power), limits
    )


class GpuReader:
    def __init__(
        self,
        *,
        sysfs: Path = SYSFS,
        query: Callable[[], GpuReading] = query_nvidia,
    ) -> None:
        self._sysfs = sysfs
        self._query = query
        self._lock = threading.Lock()
        self._limits: NvidiaLimits | None = None
        self._limits_at: float | None = None

    def read(self) -> GpuReading:
        device = find_nvidia_gpu(self._sysfs)
        if device is None:
            raise RuntimeError("No NVIDIA GPU is present on the PCI bus")
        if gpu_runtime_suspended(device):
            # A runtime-suspended GPU is powered off and produces no heat.
            # Querying it would wake it, so Auto uses the CPU alone.
            return GpuReading(None, "NVIDIA GPU powered off (runtime suspended)", True, None, None)
        reading = self._query()
        with self._lock:
            self._limits = reading.limits
            self._limits_at = time.monotonic()
        return reading

    def last_limits(self) -> NvidiaLimits | None:
        with self._lock:
            return self._limits

    def last_limits_age(self) -> float | None:
        with self._lock:
            return None if self._limits_at is None else time.monotonic() - self._limits_at


class RaplPowerMeter:
    """Average CPU package power from the RAPL energy counter (root-readable)."""

    def __init__(
        self,
        zone: Path = SYSFS / "class" / "powercap" / "intel-rapl:0",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._zone = zone
        self._clock = clock
        self._lock = threading.Lock()
        self._previous: tuple[int, float] | None = None
        self._last_power: float | None = None

    def sample(self) -> float | None:
        with self._lock:
            try:
                if (self._zone / "name").read_text(encoding="ascii").strip() != "package-0":
                    return None
                energy = int((self._zone / "energy_uj").read_text(encoding="ascii"))
                energy_range = int(
                    (self._zone / "max_energy_range_uj").read_text(encoding="ascii")
                )
            except (OSError, ValueError):
                return None
            now = self._clock()
            previous = self._previous
            if previous is not None and now - previous[1] < MIN_POWER_INTERVAL_SECONDS:
                return self._last_power
            self._previous = (energy, now)
            if previous is None:
                return None
            consumed = energy - previous[0]
            if consumed < 0:
                consumed += energy_range + 1
            power = consumed / 1_000_000 / (now - previous[1])
            self._last_power = power if 0 <= power <= MAX_PLAUSIBLE_CPU_POWER_WATTS else None
            return self._last_power


_gpu_reader = GpuReader()
_cpu_power_meter = RaplPowerMeter()


def last_nvidia_limits() -> NvidiaLimits | None:
    return _gpu_reader.last_limits()


def last_nvidia_limits_age() -> float | None:
    return _gpu_reader.last_limits_age()


def read_temperatures() -> SensorReadings:
    cpu_c, cpu_source = read_cpu_temperature()
    gpu = _gpu_reader.read()
    return SensorReadings(
        cpu_c,
        gpu.temperature,
        cpu_source,
        gpu.source,
        gpu.powered_off,
        _cpu_power_meter.sample(),
        gpu.power_w,
    )
