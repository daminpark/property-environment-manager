from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trv_regulator.calendar_policy import CalendarPolicyEvaluator
from trv_regulator.config import Settings, ZoneConfig, load_settings
from trv_regulator.controller import TRVRegulator, ZoneSnapshot
from trv_regulator.ha.client import HomeAssistantClient


def make_settings(tmp_path: Path, zone: ZoneConfig | None = None) -> Settings:
    zone = zone or ZoneConfig(
        zone_id="z",
        display_name="193 Z",
        climate_entity="climate.193_z_trv",
        room_temperature_entity="sensor.193_y_thermometer_temperature",
        absolute_humidity_entity="sensor.193_z_absolute_humidity",
        relative_humidity_entity="sensor.193_y_thermometer_humidity",
    )
    return Settings(
        house_code="193",
        zones=(zone,),
        boiler_entity="switch.193_y_boiler",
        active_control=False,
        poll_interval_seconds=60,
        base_drying_target_c=24.0,
        elevated_drying_target_c=25.0,
        severe_drying_target_c=26.0,
        drying_elevated_absolute_humidity_gm3=13.0,
        drying_severe_absolute_humidity_gm3=16.0,
        drying_recovered_absolute_humidity_gm3=12.2,
        drying_falling_rate_gm3_per_min=-0.03,
        heating_response_window_minutes=30,
        heating_min_expected_rise_c=0.2,
        window_drop_rate_c_per_hour=-1.0,
        sensor_stale_minutes=45,
        ha_url="http://example.invalid",
        ha_token="token",
        state_path=tmp_path / "state.json",
        database_path=tmp_path / "events.sqlite3",
    )


def snapshot(
    settings: Settings,
    now: datetime,
    *,
    room_temp: float = 22.0,
    target: float = 24.0,
    hvac_action: str = "idle",
    boiler_on: bool = False,
    absolute_humidity: float | None = 12.0,
    relative_humidity: float | None = 55.0,
    hvac_mode: str = "heat",
    child_lock_on: bool | None = True,
    child_lock_sample_ts: datetime | None = None,
) -> ZoneSnapshot:
    return ZoneSnapshot(
        zone=settings.zones[0],
        now=now,
        boiler_on=boiler_on,
        climate_available=True,
        room_temperature_c=room_temp,
        room_sample_ts=now,
        trv_current_temperature_c=room_temp,
        target_temperature_c=target,
        hvac_mode=hvac_mode,
        hvac_action=hvac_action,
        absolute_humidity_gm3=absolute_humidity,
        relative_humidity_percent=relative_humidity,
        humidity_sample_ts=now,
        child_lock_on=child_lock_on,
        child_lock_sample_ts=child_lock_sample_ts or now,
    )


def calendar_policy(
    settings: Settings,
    now: datetime,
    zone_id: str,
    events: dict[str, list[dict[str, object]]] | None = None,
    *,
    renovation_mode_on: bool = False,
):
    evaluator = CalendarPolicyEvaluator(
        settings,
        events_by_entity=events or {},
        now=now,
        renovation_mode_on=renovation_mode_on,
    )
    return evaluator.policy_for_zone(zone_id)


def test_drying_room_recommends_elevated_target_when_humidity_not_falling(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)

    regulator.evaluate(snapshot(settings, now, absolute_humidity=13.5))
    decision = regulator.evaluate(
        snapshot(settings, now + timedelta(minutes=20), absolute_humidity=13.7)
    )

    assert decision.mode == "drying_elevated"
    assert decision.suggested_action == "would_raise_drying_target"
    assert decision.suggested_target_temperature_c == 25.0


def test_drying_room_recommends_severe_target_for_high_absolute_humidity(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)

    decision = regulator.evaluate(snapshot(settings, now, absolute_humidity=16.5))

    assert decision.mode == "drying_severe"
    assert decision.suggested_target_temperature_c == 26.0


def test_drying_room_holds_base_target_when_recovered(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)

    decision = regulator.evaluate(snapshot(settings, now, absolute_humidity=11.8))

    assert decision.mode == "drying_recovered"
    assert decision.suggested_action == "would_hold_base_target"
    assert decision.suggested_target_temperature_c == 24.0


def test_window_open_risk_when_temperature_falls_while_heating(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)

    regulator.evaluate(
        snapshot(
            settings,
            now,
            room_temp=20.0,
            target=24.0,
            hvac_action="heating",
            boiler_on=True,
            absolute_humidity=None,
            relative_humidity=None,
        )
    )
    decision = regulator.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=30),
            room_temp=19.3,
            target=24.0,
            hvac_action="heating",
            boiler_on=True,
            absolute_humidity=None,
            relative_humidity=None,
        )
    )

    assert decision.mode == "suspected_window_open"
    assert decision.window_open_risk is True
    assert decision.suggested_action == "would_turn_off_and_retry_later"


def test_heating_observed_when_room_is_warming(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)

    regulator.evaluate(
        snapshot(
            settings,
            now,
            room_temp=20.0,
            target=24.0,
            hvac_action="heating",
            boiler_on=True,
            absolute_humidity=None,
            relative_humidity=None,
        )
    )
    decision = regulator.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=30),
            room_temp=20.4,
            target=24.0,
            hvac_action="heating",
            boiler_on=True,
            absolute_humidity=None,
            relative_humidity=None,
        )
    )

    assert decision.mode == "heating_observed"
    assert decision.window_open_risk is False
    assert decision.heating_ineffective is False


def test_missing_room_temperature_is_explicitly_unavailable(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)

    snap = snapshot(settings, now, absolute_humidity=None, relative_humidity=None)
    snap = ZoneSnapshot(
        zone=snap.zone,
        now=snap.now,
        boiler_on=snap.boiler_on,
        climate_available=snap.climate_available,
        room_temperature_c=None,
        room_sample_ts=None,
        trv_current_temperature_c=snap.trv_current_temperature_c,
        target_temperature_c=snap.target_temperature_c,
        hvac_action=snap.hvac_action,
        absolute_humidity_gm3=snap.absolute_humidity_gm3,
        relative_humidity_percent=snap.relative_humidity_percent,
        humidity_sample_ts=snap.humidity_sample_ts,
    )

    decision = regulator.evaluate(snap)

    assert decision.mode == "sensor_unavailable"
    assert "room temperature sensor unavailable" in decision.reason


def test_missing_climate_entity_is_explicitly_unavailable(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="k",
        display_name="195 K",
        climate_entity="climate.195_k_trv",
        room_temperature_entity="sensor.195_k_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    snap = snapshot(settings, now, absolute_humidity=None, relative_humidity=None)
    unavailable = ZoneSnapshot(
        zone=snap.zone,
        now=snap.now,
        boiler_on=snap.boiler_on,
        climate_available=False,
        room_temperature_c=snap.room_temperature_c,
        room_sample_ts=snap.room_sample_ts,
        trv_current_temperature_c=None,
        target_temperature_c=None,
        hvac_action=None,
        absolute_humidity_gm3=snap.absolute_humidity_gm3,
        relative_humidity_percent=snap.relative_humidity_percent,
        humidity_sample_ts=snap.humidity_sample_ts,
    )

    decision = regulator.evaluate(unavailable)

    assert decision.mode == "sensor_unavailable"
    assert decision.climate_available is False
    assert "TRV climate entity unavailable" in decision.reason


def test_load_settings_uses_canonical_entity_ids(monkeypatch) -> None:
    monkeypatch.setenv("TRV_HOUSE_CODE", "195")
    monkeypatch.setenv("TRV_ZONE_IDS", "k")
    settings = load_settings()

    zone = settings.zones[0]
    assert zone.climate_entity == "climate.195_k_trv"
    assert zone.child_lock_entity == "switch.195_k_trv_child_lock"

    monkeypatch.setenv("TRV_HOUSE_CODE", "193")
    monkeypatch.setenv("TRV_ZONE_IDS", "0")
    settings = load_settings()

    zone = settings.zones[0]
    assert zone.climate_entity == "climate.193_0_trv"
    assert zone.child_lock_entity == "switch.193_0_trv_child_lock"


def test_calendar_checkin_trigger_maps_room_three_to_ensuite(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    now = datetime(2026, 6, 3, 13, 1, tzinfo=UTC)
    policy = calendar_policy(
        settings,
        now,
        "c",
        {
            "calendar.193_3_calendar": [
                {"start": "2026-06-03", "end": "2026-06-05", "summary": "Room 3"}
            ]
        },
    )

    assert policy is not None
    assert policy.calendar_state == "calendar_checkin_trigger"
    assert policy.trigger_action == "would_set_calendar_checkin_target"
    assert policy.trigger_target_temperature_c == 18.0
    assert policy.calendar_entity_id == "calendar.193_3_calendar"


def test_calendar_policy_accepts_home_assistant_date_objects(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    now = datetime(2026, 6, 3, 13, 1, tzinfo=UTC)
    policy = calendar_policy(
        settings,
        now,
        "1",
        {
            "calendar.193_1_calendar": [
                {
                    "start": {"date": "2026-06-03"},
                    "end": {"date": "2026-06-05"},
                    "summary": "Room 1",
                }
            ]
        },
    )

    assert policy is not None
    assert policy.trigger_action == "would_set_calendar_checkin_target"


def test_home_assistant_client_normalizes_service_response_calendar_events() -> None:
    client = HomeAssistantClient("http://example.invalid", "token")

    result = client._normalize_calendar_events(
        {
            "changed_states": [],
            "service_response": {
                "calendar.193_1_calendar": {
                    "events": [{"start": "2026-06-03", "summary": "Room 1"}]
                }
            },
        }
    )

    assert result == {
        "calendar.193_1_calendar": [{"start": "2026-06-03", "summary": "Room 1"}]
    }


def test_calendar_checkout_trigger_maps_whole_home_to_local_house(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    now = datetime(2026, 6, 5, 10, 1, tzinfo=UTC)
    policy = calendar_policy(
        settings,
        now,
        "1",
        {
            "calendar.193195vbr_calendar": [
                {"start": "2026-06-03", "end": "2026-06-05", "summary": "Whole home"}
            ]
        },
    )

    assert policy is not None
    assert policy.calendar_state == "calendar_checkout_trigger"
    assert policy.trigger_action == "would_set_calendar_checkout_target"
    assert policy.trigger_target_temperature_c == 14.0


def test_calendar_policy_ignores_blocked_events(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    now = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)
    policy = calendar_policy(
        settings,
        now,
        "1",
        {
            "calendar.193_1_calendar": [
                {"start": "2026-06-03", "end": "2026-06-05", "summary": "Blocked"}
            ]
        },
    )

    assert policy is not None
    assert policy.calendar_state == "calendar_vacant"
    assert policy.active_booking is False


def test_guest_in_range_manual_target_is_not_reset_during_booking(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)
    policy = calendar_policy(
        settings,
        now,
        "1",
        {
            "calendar.193_1_calendar": [
                {"start": "2026-06-03", "end": "2026-06-05", "summary": "Room 1"}
            ]
        },
    )

    decision = regulator.evaluate(
        snapshot(
            settings,
            now,
            target=21.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        policy,
    )

    assert decision.suggested_action == "none"
    assert decision.calendar_policy_state == "calendar_occupied"
    assert decision.calendar_policy_target_temperature_c == 18.0


def test_guest_high_target_is_clamped_after_delay(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)
    policy = calendar_policy(settings, now, "1")

    regulator.evaluate(
        snapshot(
            settings,
            now,
            target=25.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        policy,
    )
    decision = regulator.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=61),
            target=25.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        calendar_policy(settings, now + timedelta(minutes=61), "1"),
    )

    assert decision.mode == "guest_limit"
    assert decision.suggested_action == "would_enforce_guest_max_target"
    assert decision.suggested_target_temperature_c == 24.0


def test_service_bathroom_reverts_to_default_after_delay(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="a",
        display_name="193 A",
        climate_entity="climate.193_a_trv",
        room_temperature_entity="sensor.193_a_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 6, 3, 8, 0, tzinfo=UTC)

    regulator.evaluate(
        snapshot(
            settings,
            now,
            target=22.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        calendar_policy(settings, now, "a"),
    )
    decision = regulator.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=241),
            target=22.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        calendar_policy(settings, now + timedelta(minutes=241), "a"),
    )

    assert decision.mode == "service_default_revert"
    assert decision.suggested_action == "would_restore_service_default"
    assert decision.suggested_target_temperature_c == 20.0


def test_force_heat_mode_is_observed_after_delay(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 6, 3, 8, 0, tzinfo=UTC)

    regulator.evaluate(
        snapshot(
            settings,
            now,
            hvac_mode="off",
            absolute_humidity=None,
            relative_humidity=None,
        )
    )
    decision = regulator.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=6),
            hvac_mode="off",
            absolute_humidity=None,
            relative_humidity=None,
        )
    )

    assert decision.mode == "force_heat_mode"
    assert decision.suggested_action == "would_set_hvac_mode_heat"


def test_child_lock_off_is_observed_after_delay(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
        child_lock_entity="switch.193_1_trv_child_lock",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 6, 3, 8, 0, tzinfo=UTC)

    decision = regulator.evaluate(
        snapshot(
            settings,
            now,
            child_lock_on=False,
            child_lock_sample_ts=now - timedelta(seconds=31),
            absolute_humidity=None,
            relative_humidity=None,
        )
    )

    assert decision.mode == "child_lock_off"
    assert decision.suggested_action == "would_restore_child_lock"


def test_renovation_mode_suppresses_guest_limit_policy(tmp_path: Path) -> None:
    zone = ZoneConfig(
        zone_id="1",
        display_name="193 1",
        climate_entity="climate.193_1_trv",
        room_temperature_entity="sensor.193_1_thermometer_temperature",
    )
    settings = make_settings(tmp_path, zone)
    regulator = TRVRegulator(settings)
    now = datetime(2026, 6, 3, 8, 0, tzinfo=UTC)
    policy = calendar_policy(settings, now, "1", renovation_mode_on=True)

    regulator.evaluate(
        snapshot(
            settings,
            now,
            target=25.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        policy,
    )
    decision = regulator.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=61),
            target=25.0,
            absolute_humidity=None,
            relative_humidity=None,
        ),
        calendar_policy(
            settings,
            now + timedelta(minutes=61),
            "1",
            renovation_mode_on=True,
        ),
    )

    assert decision.suggested_action == "none"
    assert decision.calendar_policy_suppressed_by_renovation is True
