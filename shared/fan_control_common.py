from typing import Any


AUTO_INTERVAL_SECONDS = 15.0
DEFAULT_MIN_FAN_TEMP = 35
DEFAULT_MAX_FAN_TEMP = 75
MIN_AUTO_DUTY = 30
MAX_AUTO_DUTY = 100
MAX_SAFE_AUTO_TEMP = 80
# With no valid CPU or GPU reading for this long, both fans are forced to 100%.
SENSOR_STALE_TIMEOUT_SECONDS = 30.0
SENSOR_WATCHDOG_INTERVAL_SECONDS = 5.0
LIGHTBAR_MAINTENANCE_INTERVAL_SECONDS = 15.0
LIGHTBAR_REFRESH_SECONDS = 15 * 60.0
MAX_LIGHTBAR_LEVEL = 36
LIGHTBAR_MODES = ("color", "rainbow", "off")


def validate_lightbar(mode: Any, color: Any) -> tuple[str, tuple[int, int, int]]:
    if mode not in LIGHTBAR_MODES:
        raise ValueError(f"The lightbar mode must be one of {', '.join(LIGHTBAR_MODES)}")
    try:
        levels = tuple(color)
    except TypeError:
        raise ValueError("The lightbar color must be three levels") from None
    if len(levels) != 3 or any(
        isinstance(level, bool)
        or not isinstance(level, int)
        or not 0 <= level <= MAX_LIGHTBAR_LEVEL
        for level in levels
    ):
        raise ValueError(
            f"The lightbar color must be three integers from 0 to {MAX_LIGHTBAR_LEVEL}"
        )
    return mode, (levels[0], levels[1], levels[2])


def auto_target(
    temperature: float,
    minimum_temp: int = DEFAULT_MIN_FAN_TEMP,
    maximum_temp: int = DEFAULT_MAX_FAN_TEMP,
) -> int:
    effective_maximum = min(maximum_temp, MAX_SAFE_AUTO_TEMP)
    if effective_maximum <= minimum_temp:
        return MAX_AUTO_DUTY if temperature >= effective_maximum else MIN_AUTO_DUTY
    if temperature <= minimum_temp:
        return MIN_AUTO_DUTY
    if temperature >= effective_maximum:
        return MAX_AUTO_DUTY
    position = (temperature - minimum_temp) / (effective_maximum - minimum_temp)
    duty = MIN_AUTO_DUTY + position * (MAX_AUTO_DUTY - MIN_AUTO_DUTY)
    rounded_duty = int((duty + 2.5) // 5) * 5
    return max(MIN_AUTO_DUTY, min(MAX_AUTO_DUTY, rounded_duty))
