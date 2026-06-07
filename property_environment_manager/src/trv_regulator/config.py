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
    """Entity mapping for one TRV zone."""

    zone_id: str
    display_name: str
    climate_entity: str
    room_temperature_entity: str
    absolute_humidity_entity: str | None = None
    relative_humidity_entity: str | None = None
    child_lock_entity: str | None = None

    @property
    def is_drying_zone(self) -> bool:
        return self.zone_id == "z"

    @property
    def is_guest_zone(self) -> bool:
        return self.zone_id in {"0", "1", "2", "3", "4", "5", "6", "c"}

    @property
    def is_service_bathroom_zone(self) -> bool:
        return self.zone_id in {"a", "b"}

    @property
    def is_service_kitchen_zone(self) -> bool:
        return self.zone_id == "k"


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from Home Assistant add-on options."""

    house_code: str
    zones: tuple[ZoneConfig, ...]
    boiler_entity: str
    active_control: bool
    poll_interval_seconds: int
    base_drying_target_c: float
    elevated_drying_target_c: float
    severe_drying_target_c: float
    drying_elevated_absolute_humidity_gm3: float
    drying_severe_absolute_humidity_gm3: float
    drying_recovered_absolute_humidity_gm3: float
    drying_falling_rate_gm3_per_min: float
    heating_response_window_minutes: int
    heating_min_expected_rise_c: float
    window_drop_rate_c_per_hour: float
    sensor_stale_minutes: int
    ha_url: str
    ha_token: str
    state_path: Path
    database_path: Path = Path("/data/trv_regulator_events.sqlite3")
    web_port: int = 8099
    calendar_policy_enabled: bool = True
    active_calendar_policy: bool = False
    local_timezone: str = "Europe/London"
    calendar_lookbehind_days: int = 2
    calendar_lookahead_days: int = 14
    calendar_trigger_tolerance_minutes: int = 2
    calendar_checkin_target_c: float = 18.0
    calendar_checkout_target_c: float = 14.0
    guest_min_target_c: float = 14.0
    guest_max_target_c: float = 24.0
    guest_limit_delay_minutes: int = 60
    service_bathroom_target_c: float = 20.0
    service_kitchen_target_c: float = 18.0
    service_revert_delay_minutes: int = 240
    force_heat_delay_minutes: int = 5
    child_lock_delay_seconds: int = 30
    renovation_mode_entity: str | None = None
    drying_target_entity: str | None = None

    @property
    def entity_ids(self) -> set[str]:
        ids = {self.boiler_entity}
        if self.renovation_mode_entity:
            ids.add(self.renovation_mode_entity)
        if self.drying_target_entity:
            ids.add(self.drying_target_entity)
        for zone in self.zones:
            ids.add(zone.climate_entity)
            ids.add(zone.room_temperature_entity)
            if zone.child_lock_entity:
                ids.add(zone.child_lock_entity)
            if zone.absolute_humidity_entity:
                ids.add(zone.absolute_humidity_entity)
            if zone.relative_humidity_entity:
                ids.add(zone.relative_humidity_entity)
        return ids


def _temperature_entity(house_code: str, zone_id: str) -> str:
    if zone_id == "z":
        # The drying-room thermometer is named Y in both houses.
        prefix = "y" if house_code == "193" else "x"
        return f"sensor.{house_code}_{prefix}_thermometer_temperature"
    return f"sensor.{house_code}_{zone_id}_thermometer_temperature"


def _humidity_entity(house_code: str, zone_id: str) -> str | None:
    if zone_id == "z":
        prefix = "y" if house_code == "193" else "x"
        return f"sensor.{house_code}_{prefix}_thermometer_humidity"
    if zone_id in {"a", "b", "c", "k"}:
        return f"sensor.{house_code}_{zone_id}_thermometer_humidity"
    return None


def _absolute_humidity_entity(house_code: str, zone_id: str) -> str | None:
    if zone_id in {"a", "b", "c", "k", "z"}:
        return f"sensor.{house_code}_{zone_id}_absolute_humidity"
    return None


def load_settings() -> Settings:
    house_code = os.getenv("TRV_HOUSE_CODE", "195").strip()
    zone_ids = tuple(
        zone.strip().lower()
        for zone in os.getenv("TRV_ZONE_IDS", "0,1,2,3,4,5,6,a,b,c,k,z").split(",")
        if zone.strip()
    )
    zones = tuple(
        ZoneConfig(
            zone_id=zone_id,
            display_name=f"{house_code.upper()} {zone_id.upper()}",
            climate_entity=f"climate.{house_code}_{zone_id}_trv",
            room_temperature_entity=_temperature_entity(house_code, zone_id),
            absolute_humidity_entity=_absolute_humidity_entity(house_code, zone_id),
            relative_humidity_entity=_humidity_entity(house_code, zone_id),
            child_lock_entity=f"switch.{house_code}_{zone_id}_trv_child_lock",
        )
        for zone_id in zone_ids
    )
    return Settings(
        house_code=house_code,
        zones=zones,
        boiler_entity=f"switch.{house_code}_y_boiler",
        active_control=_get_bool("TRV_ACTIVE_CONTROL", False),
        poll_interval_seconds=_get_int("TRV_POLL_INTERVAL_SECONDS", 60),
        base_drying_target_c=_get_float("TRV_BASE_DRYING_TARGET_C", 24.0),
        elevated_drying_target_c=_get_float("TRV_ELEVATED_DRYING_TARGET_C", 25.0),
        severe_drying_target_c=_get_float("TRV_SEVERE_DRYING_TARGET_C", 26.0),
        drying_elevated_absolute_humidity_gm3=_get_float(
            "TRV_DRYING_ELEVATED_AH_GM3", 13.0
        ),
        drying_severe_absolute_humidity_gm3=_get_float(
            "TRV_DRYING_SEVERE_AH_GM3", 16.0
        ),
        drying_recovered_absolute_humidity_gm3=_get_float(
            "TRV_DRYING_RECOVERED_AH_GM3", 12.2
        ),
        drying_falling_rate_gm3_per_min=_get_float(
            "TRV_DRYING_FALLING_RATE_GM3_PER_MIN", -0.03
        ),
        heating_response_window_minutes=_get_int(
            "TRV_HEATING_RESPONSE_WINDOW_MINUTES", 30
        ),
        heating_min_expected_rise_c=_get_float("TRV_HEATING_MIN_EXPECTED_RISE_C", 0.2),
        window_drop_rate_c_per_hour=_get_float("TRV_WINDOW_DROP_RATE_C_PER_HOUR", -1.0),
        sensor_stale_minutes=_get_int("TRV_SENSOR_STALE_MINUTES", 45),
        ha_url=os.getenv("TRV_HA_URL", "http://supervisor/core").rstrip("/"),
        ha_token=os.getenv("TRV_HA_TOKEN", ""),
        state_path=Path(os.getenv("TRV_STATE_PATH", "/data/trv_regulator_state.json")),
        database_path=Path(os.getenv("TRV_DATABASE_PATH", "/data/trv_regulator_events.sqlite3")),
        web_port=_get_int("TRV_WEB_PORT", 8099),
        calendar_policy_enabled=_get_bool("TRV_CALENDAR_POLICY_ENABLED", True),
        active_calendar_policy=_get_bool("TRV_ACTIVE_CALENDAR_POLICY", False),
        local_timezone=os.getenv("TRV_LOCAL_TIMEZONE", "Europe/London").strip(),
        calendar_lookbehind_days=_get_int("TRV_CALENDAR_LOOKBEHIND_DAYS", 2),
        calendar_lookahead_days=_get_int("TRV_CALENDAR_LOOKAHEAD_DAYS", 14),
        calendar_trigger_tolerance_minutes=_get_int(
            "TRV_CALENDAR_TRIGGER_TOLERANCE_MINUTES", 2
        ),
        calendar_checkin_target_c=_get_float("TRV_CALENDAR_CHECKIN_TARGET_C", 18.0),
        calendar_checkout_target_c=_get_float("TRV_CALENDAR_CHECKOUT_TARGET_C", 14.0),
        guest_min_target_c=_get_float("TRV_GUEST_MIN_TARGET_C", 14.0),
        guest_max_target_c=_get_float("TRV_GUEST_MAX_TARGET_C", 24.0),
        guest_limit_delay_minutes=_get_int("TRV_GUEST_LIMIT_DELAY_MINUTES", 60),
        service_bathroom_target_c=_get_float("TRV_SERVICE_BATHROOM_TARGET_C", 20.0),
        service_kitchen_target_c=_get_float("TRV_SERVICE_KITCHEN_TARGET_C", 18.0),
        service_revert_delay_minutes=_get_int("TRV_SERVICE_REVERT_DELAY_MINUTES", 240),
        force_heat_delay_minutes=_get_int("TRV_FORCE_HEAT_DELAY_MINUTES", 5),
        child_lock_delay_seconds=_get_int("TRV_CHILD_LOCK_DELAY_SECONDS", 30),
        renovation_mode_entity=os.getenv(
            "TRV_RENOVATION_MODE_ENTITY", f"input_boolean.renovation_mode_{house_code}"
        ).strip(),
        drying_target_entity=os.getenv(
            "TRV_DRYING_TARGET_ENTITY", f"input_number.fan_{house_code}z_trv_temperature"
        ).strip(),
    )
