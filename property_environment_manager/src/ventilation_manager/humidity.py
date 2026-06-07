"""Humidity calculations."""

from __future__ import annotations

import math


def absolute_humidity_gm3(relative_humidity: float, temperature_c: float) -> float:
    """Calculate absolute humidity in g/m3 from RH percentage and Celsius.

    Uses the Magnus approximation, which is accurate enough for room ventilation
    decisions.
    """

    saturation_hpa = 6.112 * math.exp((17.67 * temperature_c) / (temperature_c + 243.5))
    vapor_pressure_hpa = saturation_hpa * relative_humidity / 100.0
    return 216.7 * vapor_pressure_hpa / (temperature_c + 273.15)
