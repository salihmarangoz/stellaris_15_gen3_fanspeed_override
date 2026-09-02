AUTO_INTERVAL_SECONDS = 15.0
DEFAULT_MIN_FAN_TEMP = 35
DEFAULT_MAX_FAN_TEMP = 75
MIN_AUTO_DUTY = 30
MAX_AUTO_DUTY = 100
MAX_SAFE_AUTO_TEMP = 80


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
