from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ventilation_manager.config import Settings, ZoneConfig
from ventilation_manager.controller import VentilationController, ZoneSnapshot


def make_settings(tmp_path: Path) -> Settings:
    zone = ZoneConfig(
        zone_id="b",
        display_name="193 B",
        change_only_sensor=False,
        fan_entity="switch.193_b_fan",
        humidity_entity="sensor.193_b_thermometer_humidity",
        temperature_entity="sensor.193_b_thermometer_temperature",
        absolute_humidity_entity="sensor.193_b_absolute_humidity",
    )
    return Settings(
        house_code="193",
        zones=(zone,),
        active_control=False,
        poll_interval_seconds=30,
        baseline_window_minutes=90,
        baseline_margin_gm3=0.8,
        rise_delta_threshold_gm3=1.0,
        rise_rate_threshold_gm3_per_min=0.08,
        stable_rate_gm3_per_min=0.03,
        high_rh_guard_percent=75.0,
        sensor_stale_minutes=30,
        min_runtime_minutes=20,
        max_runtime_minutes=180,
        ha_url="http://example.invalid",
        ha_token="token",
        state_path=tmp_path / "state.json",
        database_path=tmp_path / "events.sqlite3",
    )


def make_change_only_settings(tmp_path: Path) -> Settings:
    settings = make_settings(tmp_path)
    zone = ZoneConfig(
        zone_id="c",
        display_name="195 C",
        change_only_sensor=True,
        fan_entity="switch.195_c_fan",
        humidity_entity="sensor.195_c_thermometer_humidity",
        temperature_entity="sensor.195_c_thermometer_temperature",
        absolute_humidity_entity="sensor.195_c_absolute_humidity",
    )
    return Settings(
        house_code="195",
        zones=(zone,),
        active_control=settings.active_control,
        poll_interval_seconds=settings.poll_interval_seconds,
        baseline_window_minutes=settings.baseline_window_minutes,
        baseline_margin_gm3=settings.baseline_margin_gm3,
        rise_delta_threshold_gm3=settings.rise_delta_threshold_gm3,
        rise_rate_threshold_gm3_per_min=settings.rise_rate_threshold_gm3_per_min,
        stable_rate_gm3_per_min=settings.stable_rate_gm3_per_min,
        high_rh_guard_percent=settings.high_rh_guard_percent,
        sensor_stale_minutes=settings.sensor_stale_minutes,
        min_runtime_minutes=settings.min_runtime_minutes,
        max_runtime_minutes=settings.max_runtime_minutes,
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        state_path=tmp_path / "change_only_state.json",
        database_path=tmp_path / "change_only_events.sqlite3",
    )


def snapshot(settings: Settings, now: datetime, **kwargs: object) -> ZoneSnapshot:
    return ZoneSnapshot(
        zone=settings.zones[0],
        now=now,
        fan_on=bool(kwargs.get("fan_on", False)),
        relative_humidity=float(kwargs["rh"]),
        temperature_c=22.0,
        absolute_humidity=float(kwargs["abs_h"]),
        sample_ts=kwargs.get("sample_ts", now),  # type: ignore[arg-type]
    )


def test_turns_on_when_absolute_humidity_rises_above_baseline(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    idle = controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    assert idle.mode == "idle"
    assert idle.should_run is False

    rising = controller.evaluate(
        snapshot(settings, now + timedelta(minutes=4), rh=66, abs_h=11.6)
    )
    assert rising.mode == "moisture_rising"
    assert rising.should_run is True
    assert rising.command == "turn_on"
    assert rising.event_baseline_absolute_humidity == 10.0



def test_reports_mismatch_when_observer_would_run_but_fan_is_off(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0, fan_on=False))
    decision = controller.evaluate(
        snapshot(settings, now + timedelta(minutes=4), rh=66, abs_h=11.6, fan_on=False)
    )

    assert decision.should_run is True
    assert decision.fan_on is False
    assert decision.fan_state_mismatch is True


def test_keeps_running_when_rate_flat_but_room_above_baseline(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    controller.evaluate(snapshot(settings, now + timedelta(minutes=3), rh=76, abs_h=12.0))

    plateau = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=35),
            fan_on=True,
            rh=78,
            abs_h=11.9,
        )
    )
    assert plateau.mode == "drying"
    assert plateau.should_run is True
    assert plateau.command == "keep_on"
    assert "target 10.80" in plateau.reason


def test_turns_off_only_after_returning_near_baseline_and_below_rh_guard(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    controller.evaluate(snapshot(settings, now + timedelta(minutes=3), rh=76, abs_h=12.0))
    decision = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=40),
            fan_on=True,
            rh=64,
            abs_h=10.6,
        )
    )

    assert decision.should_run is False
    assert decision.command == "turn_off"
    assert decision.reason == "back near baseline and stable"


def test_clears_persisted_drying_state_if_fan_is_off_and_room_is_near_baseline(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    runtime = controller.runtimes["b"]
    runtime.mode = "drying"
    runtime.baseline_absolute_humidity = 11.0
    runtime.event_baseline_absolute_humidity = 11.0
    runtime.humidity_event_started_at = now

    decision = controller.evaluate(
        snapshot(settings, now + timedelta(minutes=5), rh=58, abs_h=11.4)
    )

    assert decision.should_run is False
    assert decision.command == "turn_off"


def test_stale_sensor_does_not_start_from_old_high_reading(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    decision = controller.evaluate(
        snapshot(
            settings,
            now,
            rh=82,
            abs_h=18.0,
            sample_ts=now - timedelta(minutes=90),
        )
    )

    assert decision.mode == "sensor_stale"
    assert decision.should_run is False
    assert decision.sensor_stale is True


def test_does_not_learn_initial_baseline_from_high_humidity(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    decision = controller.evaluate(snapshot(settings, now, rh=82, abs_h=18.0))

    assert decision.mode == "learning_baseline"
    assert decision.should_run is True
    assert decision.baseline_absolute_humidity is None
    assert decision.event_baseline_absolute_humidity is None
    assert controller.runtimes["b"].humidity_event_started_at == now
    assert "no safe baseline" in decision.reason


def test_change_only_high_stale_reading_runs_conservatively(tmp_path: Path) -> None:
    settings = make_change_only_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=55, abs_h=11.0))
    decision = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=90),
            rh=82,
            abs_h=13.0,
            sample_ts=now + timedelta(minutes=31),
        )
    )

    assert decision.mode == "sensor_stale"
    assert decision.should_run is True
    assert controller.runtimes["c"].humidity_event_started_at == now + timedelta(minutes=90)
    assert "change-only sensor stale" in decision.reason



def test_does_not_turn_off_due_to_max_runtime_while_still_humid(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=55, abs_h=10.0, fan_on=False))
    controller.evaluate(snapshot(settings, now + timedelta(minutes=3), rh=80, abs_h=12.0, fan_on=True))
    decision = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=settings.max_runtime_minutes + 30),
            rh=78,
            abs_h=12.1,
            fan_on=True,
        )
    )

    assert decision.mode == "drying"
    assert decision.should_run is True
    assert decision.command == "keep_on"
    assert "max runtime" not in decision.reason
