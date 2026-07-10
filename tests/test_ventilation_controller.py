from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ventilation_manager.config import Settings, ZoneConfig
from ventilation_manager.controller import VentilationController, ZoneSnapshot
from ventilation_manager.ha.client import EntityState


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
        fan_available=bool(kwargs.get("fan_available", True)),
        relative_humidity=float(kwargs["rh"]),
        temperature_c=22.0,
        absolute_humidity=float(kwargs["abs_h"]),
        sample_ts=kwargs.get("sample_ts", now),  # type: ignore[arg-type]
    )


def entity_state(value: object, updated_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(state=str(value), last_updated=updated_at.isoformat())


def make_two_zone_settings(tmp_path: Path, *, active_control: bool) -> Settings:
    settings = make_settings(tmp_path)
    zones = tuple(
        ZoneConfig(
            zone_id=zone_id,
            display_name=f"193 {zone_id.upper()}",
            change_only_sensor=False,
            fan_entity=f"switch.193_{zone_id}_fan",
            humidity_entity=f"sensor.193_{zone_id}_humidity",
            temperature_entity=f"sensor.193_{zone_id}_temperature",
            absolute_humidity_entity=f"sensor.193_{zone_id}_absolute_humidity",
        )
        for zone_id in ("a", "b")
    )
    return replace(settings, zones=zones, active_control=active_control)


def complete_states(settings: Settings, now: datetime) -> dict[str, SimpleNamespace]:
    states: dict[str, SimpleNamespace] = {}
    for zone in settings.zones:
        states[zone.fan_entity] = entity_state("off", now)
        states[zone.humidity_entity] = entity_state(82, now)
        states[zone.temperature_entity] = entity_state(22, now)
        states[zone.absolute_humidity_entity] = entity_state(18, now)
    return states


class FakeHomeAssistant:
    def __init__(
        self,
        states: dict[str, SimpleNamespace],
        *,
        diagnostic_failure_prefix: str | None = None,
        turn_on_failure: str | None = None,
    ) -> None:
        self.states = states
        self.diagnostic_failure_prefix = diagnostic_failure_prefix
        self.turn_on_failure = turn_on_failure
        self.set_state_calls: list[str] = []
        self.switch_calls: list[tuple[str, bool]] = []

    async def get_states(self) -> dict[str, SimpleNamespace]:
        return self.states

    async def set_state(
        self, entity_id: str, state: object, attributes: dict[str, object]
    ) -> None:
        self.set_state_calls.append(entity_id)
        if self.diagnostic_failure_prefix and entity_id.startswith(
            self.diagnostic_failure_prefix
        ):
            raise RuntimeError("diagnostic write failed")

    async def set_switch_state_verified(self, entity_id: str, *, on: bool) -> None:
        self.switch_calls.append((entity_id, on))
        if on and entity_id == self.turn_on_failure:
            raise RuntimeError("fan write failed")


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

    assert decision.should_run is True
    assert decision.command == "keep_on"


def test_observer_minimum_runtime_does_not_depend_on_physical_fan_state(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    controller.evaluate(
        snapshot(settings, now + timedelta(minutes=2), rh=70, abs_h=11.5)
    )
    early = controller.evaluate(
        snapshot(settings, now + timedelta(minutes=5), rh=55, abs_h=10.4)
    )
    finished = controller.evaluate(
        snapshot(settings, now + timedelta(minutes=23), rh=55, abs_h=10.4)
    )

    assert early.should_run is True
    assert early.command == "keep_on"
    assert finished.should_run is False
    assert finished.command == "turn_off"


def test_rate_only_spike_requires_meaningful_delta_from_baseline(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    decision = controller.evaluate(
        snapshot(settings, now + timedelta(minutes=1), rh=53, abs_h=10.2)
    )

    assert decision.rate_gm3_per_min is not None
    assert decision.rate_gm3_per_min > settings.rise_rate_threshold_gm3_per_min
    assert decision.should_run is False


def test_unavailable_fan_is_a_write_blocker_not_a_state_mismatch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    decision = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=4),
            rh=70,
            abs_h=11.6,
            fan_available=False,
        )
    )

    assert decision.should_run is True
    assert decision.fan_available is False
    assert decision.fan_state_mismatch is False
    assert decision.write_blocked is True


def test_change_only_sensor_freshness_uses_source_humidity_timestamp(
    tmp_path: Path,
) -> None:
    settings = make_change_only_settings(tmp_path)
    controller = VentilationController(settings)
    zone = settings.zones[0]
    now = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=45)).isoformat()
    fresh = now.isoformat()
    states = {
        zone.fan_entity: EntityState(zone.fan_entity, "off", {}, fresh, fresh),
        zone.humidity_entity: EntityState(
            zone.humidity_entity, "72", {}, old, old
        ),
        zone.temperature_entity: EntityState(
            zone.temperature_entity, "22", {}, fresh, fresh
        ),
        zone.absolute_humidity_entity: EntityState(
            zone.absolute_humidity_entity, "14", {}, fresh, fresh
        ),
    }

    observed = controller._snapshot(zone, states, now)

    assert observed.sample_ts == datetime.fromisoformat(old)
    assert now - observed.sample_ts > timedelta(minutes=settings.sensor_stale_minutes)


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



def test_does_not_turn_off_just_because_event_has_run_for_hours(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=55, abs_h=10.0, fan_on=False))
    controller.evaluate(snapshot(settings, now + timedelta(minutes=3), rh=80, abs_h=12.0, fan_on=True))
    decision = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(hours=8),
            rh=78,
            abs_h=12.1,
            fan_on=True,
        )
    )

    assert decision.mode == "drying"
    assert decision.should_run is True
    assert decision.command == "keep_on"


def test_observer_run_publishes_diagnostics_without_device_writes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime.now(UTC).isoformat()

    class FakeHomeAssistant:
        def __init__(self) -> None:
            self.published: list[tuple[str, object, dict[str, object]]] = []

        async def get_states(self) -> dict[str, EntityState]:
            zone = settings.zones[0]
            return {
                zone.fan_entity: EntityState(
                    zone.fan_entity, "off", {}, now, now
                ),
                zone.humidity_entity: EntityState(
                    zone.humidity_entity, "52", {}, now, now
                ),
                zone.temperature_entity: EntityState(
                    zone.temperature_entity, "22", {}, now, now
                ),
                zone.absolute_humidity_entity: EntityState(
                    zone.absolute_humidity_entity, "10", {}, now, now
                ),
            }

        async def set_state(
            self, entity_id: str, state: object, attributes: dict[str, object]
        ) -> None:
            self.published.append((entity_id, state, attributes))

        async def set_switch_state_verified(self, *_: object, **__: object) -> None:
            raise AssertionError("observer mode attempted a fan write")

    fake = FakeHomeAssistant()
    decisions = asyncio.run(controller.run_once(fake))  # type: ignore[arg-type]

    assert len(decisions) == 1
    assert decisions[0].fan_available is True
    assert len(fake.published) == 8
    assert (
        f"sensor.{settings.house_code}_ventilation_manager_health"
        in {item[0] for item in fake.published}
    )
    assert all(item[2]["active_control"] is False for item in fake.published)


def test_snapshot_uses_oldest_direct_humidity_timestamp(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    zone = settings.zones[0]
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    states = {
        zone.fan_entity: entity_state("off", now),
        zone.humidity_entity: entity_state(55, now - timedelta(minutes=5)),
        zone.temperature_entity: entity_state(22, now - timedelta(hours=2)),
        zone.absolute_humidity_entity: entity_state(11.0, now - timedelta(minutes=10)),
    }

    result = controller._snapshot(zone, states, now)

    assert result.absolute_humidity == 11.0
    assert result.sample_ts == now - timedelta(minutes=10)


def test_snapshot_uses_rh_and_temperature_timestamps_for_derived_humidity(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    zone = settings.zones[0]
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    states = {
        zone.fan_entity: entity_state("off", now),
        zone.humidity_entity: entity_state(55, now - timedelta(minutes=5)),
        zone.temperature_entity: entity_state(22, now - timedelta(minutes=90)),
        zone.absolute_humidity_entity: entity_state("unavailable", now),
    }

    result = controller._snapshot(zone, states, now)

    assert result.absolute_humidity is not None
    assert result.sample_ts == now - timedelta(minutes=90)


def test_active_event_survives_stale_sensor_mode_and_turns_off(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    controller.evaluate(
        snapshot(settings, now + timedelta(minutes=3), rh=78, abs_h=12.0)
    )
    stale = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=40),
            fan_on=True,
            rh=78,
            abs_h=12.0,
            sample_ts=now + timedelta(minutes=3),
        )
    )
    recovered = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=41),
            fan_on=True,
            rh=64,
            abs_h=10.6,
        )
    )

    assert stale.mode == "sensor_stale"
    assert recovered.should_run is False
    assert recovered.command == "turn_off"


def test_active_event_survives_unavailable_sensor_mode_and_turns_off(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    zone = settings.zones[0]
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=52, abs_h=10.0))
    controller.evaluate(
        snapshot(settings, now + timedelta(minutes=3), rh=78, abs_h=12.0)
    )
    unavailable = controller.evaluate(
        ZoneSnapshot(
            zone=zone,
            now=now + timedelta(minutes=10),
            fan_on=True,
            fan_available=True,
            relative_humidity=None,
            temperature_c=None,
            absolute_humidity=None,
            sample_ts=None,
        )
    )
    recovered = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=40),
            fan_on=True,
            rh=64,
            abs_h=10.6,
        )
    )

    assert unavailable.mode == "sensor_unavailable"
    assert recovered.should_run is False
    assert recovered.command == "turn_off"


def test_no_baseline_recovery_waits_for_minimum_runtime_before_turning_off(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=82, abs_h=18.0, fan_on=False))
    before_minimum = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=10),
            rh=60,
            abs_h=11.0,
            fan_on=True,
        )
    )
    after_minimum = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=21),
            rh=60,
            abs_h=11.0,
            fan_on=True,
        )
    )

    assert before_minimum.should_run is True
    assert before_minimum.command == "keep_on"
    assert after_minimum.should_run is False
    assert after_minimum.command == "turn_off"
    assert controller.runtimes["b"].humidity_event_started_at is None


def test_no_baseline_recovery_observer_runtime_does_not_depend_on_fan_state(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    controller = VentilationController(settings)
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    controller.evaluate(snapshot(settings, now, rh=82, abs_h=18.0, fan_on=False))
    before_minimum = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=2),
            rh=60,
            abs_h=11.0,
            fan_on=False,
        )
    )
    after_minimum = controller.evaluate(
        snapshot(
            settings,
            now + timedelta(minutes=21),
            rh=60,
            abs_h=11.0,
            fan_on=False,
        )
    )

    assert before_minimum.should_run is True
    assert before_minimum.command == "keep_on"
    assert after_minimum.should_run is False
    assert after_minimum.command == "turn_off"


def test_run_once_continues_after_zone_evaluation_failure(
    tmp_path: Path, monkeypatch: object
) -> None:
    settings = make_two_zone_settings(tmp_path, active_control=False)
    controller = VentilationController(settings)
    now = datetime.now(UTC)
    ha = FakeHomeAssistant(complete_states(settings, now))
    original_evaluate = controller.evaluate

    def fail_first_zone(current: ZoneSnapshot):
        if current.zone.zone_id == "a":
            raise RuntimeError("evaluation failed")
        return original_evaluate(current)

    monkeypatch.setattr(controller, "evaluate", fail_first_zone)  # type: ignore[attr-defined]

    decisions = asyncio.run(controller.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["b"]
    assert any(entity_id.startswith("sensor.193_b_ventilation") for entity_id in ha.set_state_calls)


def test_run_once_keeps_actuation_and_later_zones_after_write_failures(
    tmp_path: Path,
) -> None:
    settings = make_two_zone_settings(tmp_path, active_control=True)
    controller = VentilationController(settings)
    now = datetime.now(UTC)
    first_zone, second_zone = settings.zones
    ha = FakeHomeAssistant(
        complete_states(settings, now),
        diagnostic_failure_prefix="sensor.193_a_ventilation",
        turn_on_failure=first_zone.fan_entity,
    )

    decisions = asyncio.run(controller.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["a", "b"]
    assert ha.switch_calls == [
        (first_zone.fan_entity, True),
        (second_zone.fan_entity, True),
    ]
    assert any(entity_id.startswith("sensor.193_b_ventilation") for entity_id in ha.set_state_calls)
