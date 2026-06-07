"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in {"", "null", "none"} else value


def _get_bool(name: str, default: bool) -> bool:
    value = _env_value(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = _env_value(name)
    return default if value is None else int(value)


def _get_float(name: str, default: float) -> float:
    value = _env_value(name)
    return default if value is None else float(value)


@dataclass(frozen=True)
class ZoneConfig:
    """Entity mapping for one ventilation zone."""

    zone_id: str
    display_name: str
    change_only_sensor: bool
    fan_entity: str
    humidity_entity: str
    temperature_entity: str
    absolute_humidity_entity: str


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from Home Assistant add-on options."""

    house_code: str
    zones: tuple[ZoneConfig, ...]
    active_control: bool
    poll_interval_seconds: int
    baseline_window_minutes: int
    baseline_margin_gm3: float
    rise_delta_threshold_gm3: float
    rise_rate_threshold_gm3_per_min: float
    stable_rate_gm3_per_min: float
    high_rh_guard_percent: float
    sensor_stale_minutes: int
    min_runtime_minutes: int
    max_runtime_minutes: int
    ha_url: str
    ha_token: str
    state_path: Path
    database_path: Path = Path("/data/ventilation_manager_events.sqlite3")
    web_port: int = 8098

    @property
    def entity_ids(self) -> set[str]:
        ids: set[str] = set()
        for zone in self.zones:
            ids.update(
                {
                    zone.fan_entity,
                    zone.humidity_entity,
                    zone.temperature_entity,
                    zone.absolute_humidity_entity,
                }
            )
        return ids


def load_settings() -> Settings:
    house_code = os.getenv("VENT_HOUSE_CODE", "195").strip()
    zone_ids = tuple(
        zone.strip().lower()
        for zone in os.getenv("VENT_ZONE_IDS", "a,b,c,k").split(",")
        if zone.strip()
    )
    change_only_zone_ids = {
        zone.strip().lower()
        for zone in os.getenv("VENT_CHANGE_ONLY_ZONE_IDS", "").split(",")
        if zone.strip()
    }
    zones = tuple(
        ZoneConfig(
            zone_id=zone_id,
            display_name=f"{house_code.upper()} {zone_id.upper()}",
            change_only_sensor=zone_id in change_only_zone_ids,
            fan_entity=f"switch.{house_code}_{zone_id}_fan",
            humidity_entity=f"sensor.{house_code}_{zone_id}_thermometer_humidity",
            temperature_entity=f"sensor.{house_code}_{zone_id}_thermometer_temperature",
            absolute_humidity_entity=f"sensor.{house_code}_{zone_id}_absolute_humidity",
        )
        for zone_id in zone_ids
    )

    return Settings(
        house_code=house_code,
        zones=zones,
        active_control=_get_bool("VENT_ACTIVE_CONTROL", False),
        poll_interval_seconds=_get_int("VENT_POLL_INTERVAL_SECONDS", 30),
        baseline_window_minutes=_get_int("VENT_BASELINE_WINDOW_MINUTES", 90),
        baseline_margin_gm3=_get_float("VENT_BASELINE_MARGIN_GM3", 0.8),
        rise_delta_threshold_gm3=_get_float("VENT_RISE_DELTA_THRESHOLD_GM3", 1.0),
        rise_rate_threshold_gm3_per_min=_get_float(
            "VENT_RISE_RATE_THRESHOLD_GM3_PER_MIN", 0.08
        ),
        stable_rate_gm3_per_min=_get_float("VENT_STABLE_RATE_GM3_PER_MIN", 0.03),
        high_rh_guard_percent=_get_float("VENT_HIGH_RH_GUARD_PERCENT", 75.0),
        sensor_stale_minutes=_get_int("VENT_SENSOR_STALE_MINUTES", 30),
        min_runtime_minutes=_get_int("VENT_MIN_RUNTIME_MINUTES", 20),
        max_runtime_minutes=_get_int("VENT_MAX_RUNTIME_MINUTES", 180),
        ha_url=os.getenv("VENT_HA_URL", "http://supervisor/core").rstrip("/"),
        ha_token=os.getenv("VENT_HA_TOKEN", ""),
        state_path=Path(os.getenv("VENT_STATE_PATH", "/data/ventilation_manager_state.json")),
        database_path=Path(os.getenv("VENT_DATABASE_PATH", "/data/ventilation_manager_events.sqlite3")),
        web_port=_get_int("VENT_WEB_PORT", 8098),
    )
