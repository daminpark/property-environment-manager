from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trv_regulator.calendar_policy import ZoneCalendarPolicy
from trv_regulator.config import Settings as TRVSettings
from trv_regulator.config import ZoneConfig as TRVZoneConfig
from trv_regulator.controller import TRVRegulator
from trv_regulator.controller import ZoneSnapshot as TRVSnapshot
from ventilation_manager.config import Settings as VentilationSettings
from ventilation_manager.config import ZoneConfig as VentilationZoneConfig
from ventilation_manager.controller import VentilationController


@dataclass(frozen=True)
class FakeState:
    state: str
    attributes: dict[str, Any]
    last_updated: str | None
    last_changed: str | None


class RecordingHA:
    def __init__(self, states: dict[str, FakeState] | None = None) -> None:
        self.states = states or {}
        self.actuator_calls: list[tuple[Any, ...]] = []

    async def get_states(self) -> dict[str, FakeState]:
        return self.states

    async def set_state(
        self, entity_id: str, state: object, attributes: dict[str, Any]
    ) -> None:
        return None

    async def set_climate_temperature(
        self, entity_id: str, temperature: float, hvac_mode: str = "heat"
    ) -> None:
        self.actuator_calls.append(
            ("set_climate_temperature", entity_id, temperature, hvac_mode)
        )

    async def set_climate_hvac_mode(self, entity_id: str, hvac_mode: str) -> None:
        self.actuator_calls.append(("set_climate_hvac_mode", entity_id, hvac_mode))

    async def set_climate_temperature_verified(
        self, entity_id: str, temperature: float
    ) -> None:
        await self.set_climate_temperature(entity_id, temperature)

    async def set_climate_hvac_mode_verified(
        self, entity_id: str, hvac_mode: str
    ) -> None:
        await self.set_climate_hvac_mode(entity_id, hvac_mode)

    async def set_switch_state_verified(self, entity_id: str, *, on: bool) -> None:
        if on:
            await self.turn_on(entity_id)
        else:
            await self.turn_off(entity_id)

    async def turn_on(self, entity_id: str) -> None:
        self.actuator_calls.append(("turn_on", entity_id))

    async def turn_off(self, entity_id: str) -> None:
        self.actuator_calls.append(("turn_off", entity_id))


class FailingFirstDiagnosticHA(RecordingHA):
    def __init__(self, states: dict[str, FakeState]) -> None:
        super().__init__(states)
        self.diagnostic_attempts = 0

    async def set_state(
        self, entity_id: str, state: object, attributes: dict[str, Any]
    ) -> None:
        self.diagnostic_attempts += 1
        if self.diagnostic_attempts == 1:
            raise RuntimeError("synthetic diagnostic failure")


class FailingFirstActuatorHA(RecordingHA):
    def __init__(self, states: dict[str, FakeState]) -> None:
        super().__init__(states)
        self.failed = False

    async def set_climate_temperature_verified(
        self, entity_id: str, temperature: float
    ) -> None:
        await super().set_climate_temperature_verified(entity_id, temperature)
        if not self.failed:
            self.failed = True
            raise RuntimeError("synthetic actuator failure")


class FailingFirstSwitchHA(RecordingHA):
    def __init__(self, states: dict[str, FakeState]) -> None:
        super().__init__(states)
        self.failed = False

    async def set_switch_state_verified(self, entity_id: str, *, on: bool) -> None:
        await super().set_switch_state_verified(entity_id, on=on)
        if not self.failed:
            self.failed = True
            raise RuntimeError("synthetic switch failure")


def trv_zone(zone_id: str) -> TRVZoneConfig:
    return TRVZoneConfig(
        zone_id=zone_id,
        display_name=f"Demo {zone_id.upper()}",
        climate_entity=f"climate.demo_{zone_id}_trv",
        room_temperature_entity=f"sensor.demo_{zone_id}_temperature",
        absolute_humidity_entity=f"sensor.demo_{zone_id}_absolute_humidity",
        relative_humidity_entity=f"sensor.demo_{zone_id}_humidity",
        child_lock_entity=f"switch.demo_{zone_id}_trv_child_lock",
    )


def trv_settings(
    tmp_path: Path,
    *,
    zones: tuple[TRVZoneConfig, ...] | None = None,
    active_control: bool,
    active_calendar_policy: bool = False,
    active_boiler_control: bool = False,
    suffix: str = "default",
) -> TRVSettings:
    return TRVSettings(
        house_code="demo",
        zones=zones or (trv_zone("z"),),
        boiler_entity="switch.demo_boiler",
        active_control=active_control,
        active_boiler_control=active_boiler_control,
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
        state_path=tmp_path / f"trv-state-{suffix}.json",
        database_path=tmp_path / f"trv-events-{suffix}.sqlite3",
        calendar_policy_enabled=False,
        active_calendar_policy=active_calendar_policy,
    )


def trv_snapshot(
    zone: TRVZoneConfig,
    now: datetime,
    *,
    target: float,
    absolute_humidity: float | None,
    sample_age: timedelta = timedelta(0),
) -> TRVSnapshot:
    sample_ts = now - sample_age
    return TRVSnapshot(
        zone=zone,
        now=now,
        boiler_on=False,
        boiler_available=True,
        climate_available=True,
        room_temperature_c=21.0,
        room_sample_ts=sample_ts,
        trv_current_temperature_c=21.0,
        target_temperature_c=target,
        hvac_mode="heat",
        hvac_action="idle",
        absolute_humidity_gm3=absolute_humidity,
        relative_humidity_percent=60.0,
        humidity_sample_ts=sample_ts,
        child_lock_on=True,
        child_lock_sample_ts=sample_ts,
    )


def calendar_trigger() -> ZoneCalendarPolicy:
    return ZoneCalendarPolicy(
        zone_id="1",
        calendar_state="calendar_checkin_trigger",
        baseline_target_temperature_c=18.0,
        trigger_action="would_set_calendar_checkin_target",
        trigger_target_temperature_c=18.0,
        hvac_mode="heat",
        reason="synthetic check-in trigger",
        active_booking=True,
        calendar_entity_id="calendar.demo_1_calendar",
        event_summary="[redacted]",
        transition_id="calendar-transition-test",
    )


def test_trv_observer_mode_never_writes(tmp_path: Path) -> None:
    settings = trv_settings(tmp_path, active_control=False)
    regulator = TRVRegulator(settings)
    snapshot = trv_snapshot(
        settings.zones[0], datetime(2026, 6, 7, tzinfo=UTC), target=24.0,
        absolute_humidity=17.0,
    )
    decision = regulator.evaluate(snapshot)
    ha = RecordingHA()

    assert decision.suggested_action == "would_raise_drying_target"
    asyncio.run(regulator._apply_decision(ha, snapshot, decision))

    assert ha.actuator_calls == []


def test_trv_active_control_refuses_stale_drying_boost(tmp_path: Path) -> None:
    settings = trv_settings(tmp_path, active_control=True)
    regulator = TRVRegulator(settings)
    snapshot = trv_snapshot(
        settings.zones[0],
        datetime(2026, 6, 7, tzinfo=UTC),
        target=24.0,
        absolute_humidity=17.0,
        sample_age=timedelta(minutes=60),
    )
    decision = regulator.evaluate(snapshot)
    ha = RecordingHA()

    assert decision.suggested_action == "would_raise_drying_target"
    assert decision.sensor_stale is True
    asyncio.run(regulator._apply_decision(ha, snapshot, decision))

    assert ha.actuator_calls == []


def test_trv_active_control_restores_base_target_after_recovery(
    tmp_path: Path,
) -> None:
    settings = trv_settings(tmp_path, active_control=True)
    regulator = TRVRegulator(settings)
    snapshot = trv_snapshot(
        settings.zones[0], datetime(2026, 6, 7, tzinfo=UTC), target=26.0,
        absolute_humidity=11.8,
    )
    decision = regulator.evaluate(snapshot)
    ha = RecordingHA()

    assert decision.suggested_action == "would_hold_base_target"
    assert decision.suggested_target_temperature_c == 24.0
    asyncio.run(regulator._apply_decision(ha, snapshot, decision))

    assert ha.actuator_calls == [
        ("set_climate_temperature", "climate.demo_z_trv", 24.0, "heat")
    ]


def test_trv_calendar_actions_require_both_write_gates(tmp_path: Path) -> None:
    now = datetime(2026, 6, 7, tzinfo=UTC)
    for active_control, active_calendar_policy in (
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ):
        suffix = f"{active_control}-{active_calendar_policy}"
        zone = trv_zone("1")
        settings = trv_settings(
            tmp_path,
            zones=(zone,),
            active_control=active_control,
            active_calendar_policy=active_calendar_policy,
            suffix=suffix,
        )
        regulator = TRVRegulator(settings)
        snapshot = trv_snapshot(
            zone, now, target=14.0, absolute_humidity=None
        )
        decision = regulator.evaluate(snapshot, calendar_trigger())
        ha = RecordingHA()

        assert decision.suggested_action == "would_set_calendar_checkin_target"
        asyncio.run(regulator._apply_decision(ha, snapshot, decision))

        expected = (
            [("set_climate_temperature", "climate.demo_1_trv", 18.0, "heat")]
            if active_control and active_calendar_policy
            else []
        )
        assert ha.actuator_calls == expected, (
            active_control,
            active_calendar_policy,
        )
        assert regulator.runtimes["1"].completed_calendar_transition_id == (
            "calendar-transition-test"
            if active_control and active_calendar_policy
            else None
        )


def trv_run_states(zones: tuple[TRVZoneConfig, ...]) -> dict[str, FakeState]:
    now = datetime.now(UTC)
    fresh = now.isoformat()
    old_child_lock = (now - timedelta(minutes=1)).isoformat()
    states: dict[str, FakeState] = {
        "switch.demo_boiler": FakeState("off", {}, fresh, fresh)
    }
    for zone in zones:
        target = 24.0 if zone.zone_id == "z" else 18.0
        states[zone.climate_entity] = FakeState(
            state="heat",
            attributes={
                "current_temperature": 21.0,
                "temperature": target,
                "hvac_action": "idle",
            },
            last_updated=fresh,
            last_changed=fresh,
        )
        states[zone.room_temperature_entity] = FakeState(
            state="21.0",
            attributes={},
            last_updated=fresh,
            last_changed=fresh,
        )
        states[zone.absolute_humidity_entity or ""] = FakeState(
            state="17.0" if zone.zone_id == "z" else "10.0",
            attributes={},
            last_updated=fresh,
            last_changed=fresh,
        )
        states[zone.relative_humidity_entity or ""] = FakeState(
            state="70.0",
            attributes={},
            last_updated=fresh,
            last_changed=fresh,
        )
        states[zone.child_lock_entity or ""] = FakeState(
            state="on" if zone.zone_id == "z" else "off",
            attributes={},
            last_updated=fresh,
            last_changed=fresh if zone.zone_id == "z" else old_child_lock,
        )
    return states


def test_trv_diagnostic_failure_does_not_block_actuation_or_later_zones(
    tmp_path: Path,
) -> None:
    zones = (trv_zone("z"), trv_zone("1"))
    settings = trv_settings(
        tmp_path,
        zones=zones,
        active_control=True,
        active_calendar_policy=True,
        suffix="diagnostic-failure",
    )
    regulator = TRVRegulator(settings)
    ha = FailingFirstDiagnosticHA(trv_run_states(zones))

    decisions = asyncio.run(regulator.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["z", "1"]
    assert ha.actuator_calls == [
        ("set_climate_temperature", "climate.demo_z_trv", 26.0, "heat"),
        ("turn_on", "switch.demo_1_trv_child_lock"),
    ]


def test_trv_actuator_failure_does_not_block_later_zones(tmp_path: Path) -> None:
    zones = (trv_zone("z"), trv_zone("1"))
    settings = trv_settings(
        tmp_path,
        zones=zones,
        active_control=True,
        active_calendar_policy=True,
        suffix="actuator-failure",
    )
    regulator = TRVRegulator(settings)
    ha = FailingFirstActuatorHA(trv_run_states(zones))

    decisions = asyncio.run(regulator.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["z", "1"]
    assert ha.actuator_calls == [
        ("set_climate_temperature", "climate.demo_z_trv", 26.0, "heat"),
        ("turn_on", "switch.demo_1_trv_child_lock"),
    ]


def test_trv_evaluation_failure_blocks_boiler_off_and_later_zones_continue(
    tmp_path: Path,
) -> None:
    zones = (trv_zone("z"), trv_zone("1"))
    settings = trv_settings(
        tmp_path,
        zones=zones,
        active_control=True,
        active_calendar_policy=True,
        active_boiler_control=True,
        suffix="evaluation-failure",
    )

    class FailingFirstEvaluationRegulator(TRVRegulator):
        def evaluate(self, snapshot, calendar_policy=None):  # type: ignore[no-untyped-def]
            if snapshot.zone.zone_id == "z":
                raise RuntimeError("synthetic evaluation failure")
            return super().evaluate(snapshot, calendar_policy)

    states = trv_run_states(zones)
    boiler = states[settings.boiler_entity]
    states[settings.boiler_entity] = FakeState(
        "on", boiler.attributes, boiler.last_updated, boiler.last_changed
    )
    regulator = FailingFirstEvaluationRegulator(settings)
    ha = RecordingHA(states)

    decisions = asyncio.run(regulator.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["1"]
    assert ha.actuator_calls == [("turn_on", "switch.demo_1_trv_child_lock")]
    assert regulator.last_boiler_decision is not None
    assert (
        regulator.last_boiler_decision.suggested_action
        == "blocked_turn_off_trv_unavailable"
    )
    assert regulator.last_boiler_decision.unavailable_zone_ids == ("z",)
    assert regulator.control_errors["z"].startswith("evaluation RuntimeError")


def ventilation_zone(zone_id: str) -> VentilationZoneConfig:
    return VentilationZoneConfig(
        zone_id=zone_id,
        display_name=f"Demo {zone_id.upper()}",
        change_only_sensor=False,
        fan_entity=f"switch.demo_{zone_id}_fan",
        humidity_entity=f"sensor.demo_{zone_id}_humidity",
        temperature_entity=f"sensor.demo_{zone_id}_temperature",
        absolute_humidity_entity=f"sensor.demo_{zone_id}_absolute_humidity",
    )


def ventilation_settings(
    tmp_path: Path, zones: tuple[VentilationZoneConfig, ...]
) -> VentilationSettings:
    return VentilationSettings(
        house_code="demo",
        zones=zones,
        active_control=True,
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
        state_path=tmp_path / "ventilation-state.json",
        database_path=tmp_path / "ventilation-events.sqlite3",
    )


def ventilation_run_states(
    zones: tuple[VentilationZoneConfig, ...],
) -> dict[str, FakeState]:
    fresh = datetime.now(UTC).isoformat()
    states: dict[str, FakeState] = {}
    for zone in zones:
        states[zone.fan_entity] = FakeState("off", {}, fresh, fresh)
        states[zone.humidity_entity] = FakeState("80.0", {}, fresh, fresh)
        states[zone.temperature_entity] = FakeState("22.0", {}, fresh, fresh)
        states[zone.absolute_humidity_entity] = FakeState("12.0", {}, fresh, fresh)
    return states


def test_ventilation_diagnostic_failure_does_not_block_actuation_or_later_zones(
    tmp_path: Path,
) -> None:
    zones = (ventilation_zone("a"), ventilation_zone("b"))
    settings = ventilation_settings(tmp_path, zones)
    controller = VentilationController(settings)
    for runtime in controller.runtimes.values():
        runtime.baseline_absolute_humidity = 10.0
    ha = FailingFirstDiagnosticHA(ventilation_run_states(zones))

    decisions = asyncio.run(controller.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["a", "b"]
    assert ha.actuator_calls == [
        ("turn_on", "switch.demo_a_fan"),
        ("turn_on", "switch.demo_b_fan"),
    ]


def test_ventilation_actuator_failure_does_not_block_later_zones(
    tmp_path: Path,
) -> None:
    zones = (ventilation_zone("a"), ventilation_zone("b"))
    settings = ventilation_settings(tmp_path, zones)
    controller = VentilationController(settings)
    for runtime in controller.runtimes.values():
        runtime.baseline_absolute_humidity = 10.0
    ha = FailingFirstSwitchHA(ventilation_run_states(zones))

    decisions = asyncio.run(controller.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["a", "b"]
    assert ha.actuator_calls == [
        ("turn_on", "switch.demo_a_fan"),
        ("turn_on", "switch.demo_b_fan"),
    ]
    assert "a" in controller.control_errors
    assert "b" not in controller.control_errors


def test_ventilation_evaluation_failure_is_reported_and_later_zones_continue(
    tmp_path: Path,
) -> None:
    zones = (ventilation_zone("a"), ventilation_zone("b"))
    settings = ventilation_settings(tmp_path, zones)

    class FailingFirstEvaluationController(VentilationController):
        def evaluate(self, snapshot):  # type: ignore[no-untyped-def]
            if snapshot.zone.zone_id == "a":
                raise RuntimeError("synthetic evaluation failure")
            return super().evaluate(snapshot)

    controller = FailingFirstEvaluationController(settings)
    for runtime in controller.runtimes.values():
        runtime.baseline_absolute_humidity = 10.0
    ha = RecordingHA(ventilation_run_states(zones))

    decisions = asyncio.run(controller.run_once(ha))

    assert [decision.zone_id for decision in decisions] == ["b"]
    assert ha.actuator_calls == [("turn_on", "switch.demo_b_fan")]
    assert controller.control_errors["a"].startswith("evaluation RuntimeError")
