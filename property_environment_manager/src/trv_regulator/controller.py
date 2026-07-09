"""TRV observer and future active-control state machine."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from trv_regulator.calendar_policy import (
    CalendarPolicyEvaluator,
    ZoneCalendarPolicy,
    calendar_entities_for_house,
)
from trv_regulator.config import Settings, ZoneConfig
from trv_regulator.event_store import EventStore

if TYPE_CHECKING:
    from trv_regulator.ha.client import EntityState, HomeAssistantClient

LOGGER = logging.getLogger(__name__)


def parse_ha_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def as_float(state: EntityState | None) -> float | None:
    if state is None or state.state in {"unknown", "unavailable", "none", ""}:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def attr_float(state: EntityState | None, name: str) -> float | None:
    if state is None:
        return None
    value = state.attributes.get(name)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def attr_str(state: EntityState | None, name: str) -> str | None:
    if state is None:
        return None
    value = state.attributes.get(name)
    return None if value is None else str(value)


@dataclass
class Sample:
    """One sampled room state."""

    ts: datetime
    room_temperature_c: float | None
    trv_current_temperature_c: float | None
    target_temperature_c: float | None
    hvac_mode: str | None
    hvac_action: str | None
    boiler_on: bool
    absolute_humidity_gm3: float | None
    relative_humidity_percent: float | None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "room_temperature_c": self.room_temperature_c,
            "trv_current_temperature_c": self.trv_current_temperature_c,
            "target_temperature_c": self.target_temperature_c,
            "hvac_mode": self.hvac_mode,
            "hvac_action": self.hvac_action,
            "boiler_on": self.boiler_on,
            "absolute_humidity_gm3": self.absolute_humidity_gm3,
            "relative_humidity_percent": self.relative_humidity_percent,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Sample":
        return cls(
            ts=datetime.fromisoformat(data["ts"]).astimezone(UTC),
            room_temperature_c=data.get("room_temperature_c"),
            trv_current_temperature_c=data.get("trv_current_temperature_c"),
            target_temperature_c=data.get("target_temperature_c"),
            hvac_mode=data.get("hvac_mode"),
            hvac_action=data.get("hvac_action"),
            boiler_on=bool(data.get("boiler_on", False)),
            absolute_humidity_gm3=data.get("absolute_humidity_gm3"),
            relative_humidity_percent=data.get("relative_humidity_percent"),
        )


@dataclass
class ZoneRuntime:
    """Persistent observer state for one zone."""

    zone_id: str
    samples: list[Sample] = field(default_factory=list)
    last_mode: str = "starting"
    last_reason: str = "starting"

    def to_json(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "samples": [sample.to_json() for sample in self.samples],
            "last_mode": self.last_mode,
            "last_reason": self.last_reason,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ZoneRuntime":
        runtime = cls(zone_id=str(data["zone_id"]))
        runtime.samples = [Sample.from_json(item) for item in data.get("samples", [])]
        runtime.last_mode = str(data.get("last_mode", "restored"))
        runtime.last_reason = str(data.get("last_reason", "restored"))
        return runtime


@dataclass(frozen=True)
class ZoneSnapshot:
    """Current HA-derived state for one zone."""

    zone: ZoneConfig
    now: datetime
    boiler_on: bool
    boiler_available: bool
    climate_available: bool
    room_temperature_c: float | None
    room_sample_ts: datetime | None
    trv_current_temperature_c: float | None
    target_temperature_c: float | None
    hvac_action: str | None
    absolute_humidity_gm3: float | None
    relative_humidity_percent: float | None
    humidity_sample_ts: datetime | None
    hvac_mode: str | None = None
    child_lock_on: bool | None = None
    child_lock_sample_ts: datetime | None = None


@dataclass(frozen=True)
class ZoneDecision:
    """Observer recommendation and diagnostics."""

    zone_id: str
    mode: str
    suggested_action: str
    suggested_target_temperature_c: float | None
    reason: str
    room_temperature_c: float | None
    trv_current_temperature_c: float | None
    target_temperature_c: float | None
    hvac_mode: str | None
    hvac_action: str | None
    child_lock_on: bool | None
    boiler_on: bool
    boiler_available: bool
    room_temperature_rate_c_per_hour: float | None
    heating_response_c: float | None
    window_open_risk: bool
    heating_ineffective: bool
    absolute_humidity_gm3: float | None
    relative_humidity_percent: float | None
    absolute_humidity_rate_gm3_per_min: float | None
    sensor_stale: bool
    active_control: bool
    climate_available: bool
    room_sample_age_minutes: float | None
    humidity_sample_age_minutes: float | None
    child_lock_age_seconds: float | None
    calendar_policy_state: str | None
    calendar_policy_target_temperature_c: float | None
    calendar_policy_action: str | None
    calendar_policy_reason: str | None
    calendar_policy_active_booking: bool | None
    calendar_policy_entity_id: str | None
    calendar_policy_event_summary: str | None
    calendar_policy_suppressed_by_renovation: bool | None


@dataclass(frozen=True)
class BoilerDecision:
    """Aggregate boiler recommendation across every configured TRV."""

    zone_id: str
    mode: str
    suggested_action: str
    reason: str
    boiler_entity: str
    boiler_on: bool
    boiler_available: bool
    boiler_should_be_on: bool
    demanding_zone_ids: tuple[str, ...]
    unavailable_zone_ids: tuple[str, ...]
    control_safe: bool
    state_mismatch: bool
    active_control: bool


class TRVRegulator:
    """Observer-first TRV regulator."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtimes = {
            zone.zone_id: ZoneRuntime(zone_id=zone.zone_id) for zone in settings.zones
        }
        self.last_decisions: list[ZoneDecision] = []
        self.last_boiler_decision: BoilerDecision | None = None
        self.last_run_at: datetime | None = None
        self.control_errors: dict[str, str] = {}
        self.store = EventStore(settings.database_path)
        self.load_state()

    def load_state(self) -> None:
        path = self.settings.state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for zone_id, runtime_data in data.get("zones", {}).items():
                self.runtimes[zone_id] = ZoneRuntime.from_json(runtime_data)
        except Exception:
            LOGGER.exception("Failed to load state from %s", path)

    def save_state(self) -> None:
        path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "saved_at": datetime.now(UTC).isoformat(),
            "zones": {
                zone_id: runtime.to_json()
                for zone_id, runtime in sorted(self.runtimes.items())
            },
        }
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    async def run_once(self, ha: HomeAssistantClient) -> list[ZoneDecision]:
        states = await ha.get_states()
        now = datetime.now(UTC)
        boiler_state = states.get(self.settings.boiler_entity)
        boiler_on = boiler_state is not None and boiler_state.state == "on"
        boiler_available = boiler_state is not None and boiler_state.state in {
            "on",
            "off",
        }
        calendar_policy = await self._calendar_policy_evaluator(ha, states, now)
        decisions: list[ZoneDecision] = []
        for zone in self.settings.zones:
            snapshot = self._snapshot(
                zone, states, now, boiler_on, boiler_available
            )
            policy = (
                calendar_policy.policy_for_zone(zone.zone_id)
                if calendar_policy is not None
                else None
            )
            decision = self.evaluate(snapshot, policy)
            decisions.append(decision)
            await self._publish_diagnostics(ha, snapshot, decision)
            try:
                await self._apply_decision(ha, snapshot, decision)
                self.control_errors.pop(zone.zone_id, None)
            except Exception as exc:
                self.control_errors[zone.zone_id] = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("TRV command failed for zone %s", zone.zone_id)
        boiler_decision = self._boiler_decision(
            decisions,
            boiler_on=boiler_on,
            boiler_available=boiler_available,
        )
        await self._publish_boiler_diagnostics(ha, boiler_decision)
        try:
            await self._apply_boiler_decision(ha, boiler_decision)
            self.control_errors.pop("boiler", None)
        except Exception as exc:
            self.control_errors["boiler"] = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Boiler command failed")
        await self._publish_health(ha, decisions, boiler_decision)
        self.last_decisions = decisions
        self.last_boiler_decision = boiler_decision
        self.last_run_at = now
        self.save_state()
        self.store.record_run([*decisions, boiler_decision], run_at=now)
        return decisions

    async def _calendar_policy_evaluator(
        self,
        ha: HomeAssistantClient,
        states: dict[str, EntityState],
        now: datetime,
    ) -> CalendarPolicyEvaluator | None:
        if not self.settings.calendar_policy_enabled:
            return None
        start = now - timedelta(days=self.settings.calendar_lookbehind_days)
        end = now + timedelta(days=self.settings.calendar_lookahead_days)
        renovation_on = False
        if self.settings.renovation_mode_entity:
            renovation = states.get(self.settings.renovation_mode_entity)
            renovation_on = renovation is not None and renovation.state == "on"
        try:
            events = await ha.get_calendar_events(
                calendar_entities_for_house(self.settings.house_code),
                start_date_time=start.isoformat(),
                end_date_time=end.isoformat(),
            )
        except Exception:
            LOGGER.exception("Failed to fetch calendar policy events")
            return None
        return CalendarPolicyEvaluator(
            self.settings,
            events_by_entity=events,
            now=now,
            renovation_mode_on=renovation_on,
        )

    def evaluate(
        self,
        snapshot: ZoneSnapshot,
        calendar_policy: ZoneCalendarPolicy | None = None,
    ) -> ZoneDecision:
        runtime = self.runtimes.setdefault(
            snapshot.zone.zone_id, ZoneRuntime(zone_id=snapshot.zone.zone_id)
        )
        sample = Sample(
            ts=snapshot.now,
            room_temperature_c=snapshot.room_temperature_c,
            trv_current_temperature_c=snapshot.trv_current_temperature_c,
            target_temperature_c=snapshot.target_temperature_c,
            hvac_mode=snapshot.hvac_mode,
            hvac_action=snapshot.hvac_action,
            boiler_on=snapshot.boiler_on,
            absolute_humidity_gm3=snapshot.absolute_humidity_gm3,
            relative_humidity_percent=snapshot.relative_humidity_percent,
        )
        self._append_sample(runtime, sample)

        room_rate = self._temperature_rate(runtime)
        heating_response = self._heating_response(runtime)
        humidity_rate = self._humidity_rate(runtime)
        sensor_stale = self._sensor_stale(snapshot)

        if not snapshot.climate_available:
            return self._decision(
                snapshot,
                runtime,
                "sensor_unavailable",
                "none",
                None,
                "TRV climate entity unavailable",
                room_rate,
                heating_response,
                humidity_rate,
                sensor_stale=True,
                calendar_policy=calendar_policy,
            )

        force_heat_decision = self._force_heat_decision(
            snapshot, runtime, room_rate, heating_response, humidity_rate, sensor_stale, calendar_policy
        )
        if force_heat_decision is not None:
            return force_heat_decision

        child_lock_decision = self._child_lock_decision(
            snapshot, runtime, room_rate, heating_response, humidity_rate, sensor_stale, calendar_policy
        )
        if child_lock_decision is not None:
            return child_lock_decision

        if snapshot.room_temperature_c is None or snapshot.room_sample_ts is None:
            return self._decision(
                snapshot,
                runtime,
                "sensor_unavailable",
                "none",
                snapshot.target_temperature_c,
                "room temperature sensor unavailable; cannot assess heating response",
                room_rate,
                heating_response,
                humidity_rate,
                sensor_stale=True,
                calendar_policy=calendar_policy,
            )

        if snapshot.zone.is_drying_zone:
            drying_decision = self._drying_decision(
                snapshot, runtime, room_rate, heating_response, humidity_rate, sensor_stale
            )
            if drying_decision is not None:
                return drying_decision

        window_open = self._window_open_risk(snapshot, room_rate, heating_response)
        ineffective = self._heating_ineffective(snapshot, heating_response)
        if window_open:
            return self._decision(
                snapshot,
                runtime,
                "suspected_window_open",
                "would_turn_off_and_retry_later",
                None,
                self._window_reason(room_rate, heating_response),
                room_rate,
                heating_response,
                humidity_rate,
                sensor_stale,
                window_open_risk=True,
                heating_ineffective=ineffective,
                calendar_policy=calendar_policy,
            )
        if ineffective:
            return self._decision(
                snapshot,
                runtime,
                "heating_ineffective",
                "would_pause_and_retry_later",
                None,
                (
                    "TRV and boiler appear to be heating, but room temperature "
                    f"rose only {heating_response:.2f}C in the response window"
                ),
                room_rate,
                heating_response,
                humidity_rate,
                sensor_stale,
                heating_ineffective=True,
                calendar_policy=calendar_policy,
            )

        policy_decision = self._calendar_or_limit_decision(
            snapshot, runtime, room_rate, heating_response, humidity_rate, sensor_stale, calendar_policy
        )
        if policy_decision is not None:
            return policy_decision

        mode = "heating_observed" if snapshot.hvac_action == "heating" else "idle"
        reason = (
            "heating demand observed"
            if mode == "heating_observed"
            else (
                calendar_policy.reason
                if calendar_policy is not None
                else "no intervention suggested"
            )
        )
        return self._decision(
            snapshot,
            runtime,
            mode,
            "none",
            snapshot.target_temperature_c,
            reason,
            room_rate,
            heating_response,
            humidity_rate,
            sensor_stale,
            calendar_policy=calendar_policy,
        )

    def _snapshot(
        self,
        zone: ZoneConfig,
        states: dict[str, EntityState],
        now: datetime,
        boiler_on: bool,
        boiler_available: bool,
    ) -> ZoneSnapshot:
        climate = states.get(zone.climate_entity)
        room_temp = states.get(zone.room_temperature_entity)
        abs_humidity = (
            states.get(zone.absolute_humidity_entity)
            if zone.absolute_humidity_entity
            else None
        )
        rel_humidity = (
            states.get(zone.relative_humidity_entity)
            if zone.relative_humidity_entity
            else None
        )
        child_lock = states.get(zone.child_lock_entity) if zone.child_lock_entity else None
        return ZoneSnapshot(
            zone=zone,
            now=now,
            boiler_on=boiler_on,
            boiler_available=boiler_available,
            climate_available=climate is not None
            and climate.state not in {"unknown", "unavailable"},
            room_temperature_c=as_float(room_temp),
            room_sample_ts=parse_ha_timestamp(room_temp.last_updated if room_temp else None),
            trv_current_temperature_c=attr_float(climate, "current_temperature"),
            target_temperature_c=attr_float(climate, "temperature"),
            hvac_mode=climate.state if climate else None,
            hvac_action=attr_str(climate, "hvac_action"),
            absolute_humidity_gm3=as_float(abs_humidity),
            relative_humidity_percent=as_float(rel_humidity),
            humidity_sample_ts=parse_ha_timestamp(
                abs_humidity.last_updated if abs_humidity else None
            ),
            child_lock_on=None if child_lock is None else child_lock.state == "on",
            child_lock_sample_ts=parse_ha_timestamp(
                child_lock.last_changed if child_lock else None
            ),
        )

    def _drying_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
    ) -> ZoneDecision | None:
        absolute_humidity = snapshot.absolute_humidity_gm3
        if absolute_humidity is None:
            return self._decision(
                snapshot,
                runtime,
                "sensor_unavailable",
                "none",
                self.settings.base_drying_target_c,
                "drying-room absolute humidity unavailable",
                room_rate,
                heating_response,
                humidity_rate,
                sensor_stale=True,
            )

        falling_fast_enough = (
            humidity_rate is not None
            and humidity_rate <= self.settings.drying_falling_rate_gm3_per_min
        )
        target = self.settings.base_drying_target_c
        mode = "drying_recovered"
        action = "would_hold_base_target"
        reason = (
            f"drying room recovered; absolute humidity {absolute_humidity:.2f} g/m3"
        )

        if absolute_humidity >= self.settings.drying_severe_absolute_humidity_gm3:
            target = self.settings.severe_drying_target_c
            mode = "drying_severe"
            action = "would_raise_drying_target"
            reason = (
                f"severe drying moisture {absolute_humidity:.2f} g/m3; "
                f"rate {self._format_rate(humidity_rate)}"
            )
        elif (
            absolute_humidity >= self.settings.drying_elevated_absolute_humidity_gm3
            and not falling_fast_enough
        ):
            target = self.settings.elevated_drying_target_c
            mode = "drying_elevated"
            action = "would_raise_drying_target"
            reason = (
                f"elevated drying moisture {absolute_humidity:.2f} g/m3 is not "
                f"falling fast enough; rate {self._format_rate(humidity_rate)}"
            )
        elif absolute_humidity > self.settings.drying_recovered_absolute_humidity_gm3:
            mode = "drying_watch"
            action = "would_hold_base_target"
            reason = (
                f"drying moisture {absolute_humidity:.2f} g/m3 above recovered "
                f"threshold but falling; rate {self._format_rate(humidity_rate)}"
            )

        return self._decision(
            snapshot,
            runtime,
            mode,
            action,
            target,
            reason,
            room_rate,
            heating_response,
            humidity_rate,
            sensor_stale,
        )

    def _append_sample(self, runtime: ZoneRuntime, sample: Sample) -> None:
        runtime.samples.append(sample)
        cutoff = sample.ts - timedelta(
            minutes=max(
                90,
                self.settings.heating_response_window_minutes * 3,
                self.settings.guest_limit_delay_minutes + 10,
                self.settings.service_revert_delay_minutes + 10,
            )
        )
        runtime.samples = [item for item in runtime.samples if item.ts >= cutoff]

    def _temperature_rate(self, runtime: ZoneRuntime) -> float | None:
        samples = [s for s in runtime.samples if s.room_temperature_c is not None]
        if len(samples) < 2:
            return None
        first = samples[0]
        last = samples[-1]
        hours = (last.ts - first.ts).total_seconds() / 3600
        if hours <= 0:
            return None
        return (last.room_temperature_c - first.room_temperature_c) / hours

    def _heating_response(self, runtime: ZoneRuntime) -> float | None:
        heating_samples = [
            sample
            for sample in runtime.samples
            if sample.room_temperature_c is not None
            and sample.hvac_action == "heating"
            and sample.boiler_on
        ]
        if len(heating_samples) < 2:
            return None
        last = heating_samples[-1]
        window_start = last.ts - timedelta(
            minutes=self.settings.heating_response_window_minutes
        )
        candidates = [sample for sample in heating_samples if sample.ts <= window_start]
        first = candidates[-1] if candidates else heating_samples[0]
        minutes = (last.ts - first.ts).total_seconds() / 60
        if minutes < self.settings.heating_response_window_minutes * 0.75:
            return None
        assert first.room_temperature_c is not None
        assert last.room_temperature_c is not None
        return last.room_temperature_c - first.room_temperature_c

    def _humidity_rate(self, runtime: ZoneRuntime) -> float | None:
        samples = [s for s in runtime.samples if s.absolute_humidity_gm3 is not None]
        if len(samples) < 2:
            return None
        first = samples[0]
        last = samples[-1]
        minutes = (last.ts - first.ts).total_seconds() / 60
        if minutes <= 0:
            return None
        assert first.absolute_humidity_gm3 is not None
        assert last.absolute_humidity_gm3 is not None
        return (last.absolute_humidity_gm3 - first.absolute_humidity_gm3) / minutes

    def _sensor_stale(self, snapshot: ZoneSnapshot) -> bool:
        timestamps = [snapshot.room_sample_ts]
        if snapshot.zone.is_drying_zone:
            timestamps.append(snapshot.humidity_sample_ts)
        return any(
            ts is None
            or snapshot.now - ts > timedelta(minutes=self.settings.sensor_stale_minutes)
            for ts in timestamps
        )

    def _window_open_risk(
        self,
        snapshot: ZoneSnapshot,
        room_rate: float | None,
        heating_response: float | None,
    ) -> bool:
        if snapshot.hvac_action != "heating" or not snapshot.boiler_on:
            return False
        if room_rate is not None and room_rate <= self.settings.window_drop_rate_c_per_hour:
            return True
        return self._heating_ineffective(snapshot, heating_response)

    def _heating_ineffective(
        self, snapshot: ZoneSnapshot, heating_response: float | None
    ) -> bool:
        if (
            snapshot.hvac_action != "heating"
            or not snapshot.boiler_on
            or heating_response is None
        ):
            return False
        if snapshot.target_temperature_c is None or snapshot.room_temperature_c is None:
            return False
        if snapshot.room_temperature_c >= snapshot.target_temperature_c - 0.5:
            return False
        return heating_response < self.settings.heating_min_expected_rise_c

    def _window_reason(
        self, room_rate: float | None, heating_response: float | None
    ) -> str:
        if room_rate is not None and room_rate <= self.settings.window_drop_rate_c_per_hour:
            return f"room temperature is falling while heating; rate {room_rate:.2f}C/hour"
        if heating_response is not None:
            return (
                "room did not warm enough while TRV and boiler were heating; "
                f"response {heating_response:.2f}C"
            )
        return "possible window open while heating"

    def _force_heat_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
        calendar_policy: ZoneCalendarPolicy | None,
    ) -> ZoneDecision | None:
        if snapshot.hvac_mode not in {"auto", "off"}:
            return None
        since = self._sample_condition_since(
            runtime, lambda sample: sample.hvac_mode in {"auto", "off"}
        )
        if since is None:
            return None
        if snapshot.now - since < timedelta(minutes=self.settings.force_heat_delay_minutes):
            return None
        return self._decision(
            snapshot,
            runtime,
            "force_heat_mode",
            "would_set_hvac_mode_heat",
            snapshot.target_temperature_c,
            (
                f"TRV HVAC mode has been {snapshot.hvac_mode} for at least "
                f"{self.settings.force_heat_delay_minutes}m; HA would set it back to heat"
            ),
            room_rate,
            heating_response,
            humidity_rate,
            sensor_stale,
            calendar_policy=calendar_policy,
        )

    def _child_lock_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
        calendar_policy: ZoneCalendarPolicy | None,
    ) -> ZoneDecision | None:
        if snapshot.child_lock_on is not False or snapshot.child_lock_sample_ts is None:
            return None
        age = snapshot.now - snapshot.child_lock_sample_ts
        if age < timedelta(seconds=self.settings.child_lock_delay_seconds):
            return None
        return self._decision(
            snapshot,
            runtime,
            "child_lock_off",
            "would_restore_child_lock",
            snapshot.target_temperature_c,
            (
                f"TRV child lock has been off for {age.total_seconds():.0f}s; "
                "HA would turn it back on"
            ),
            room_rate,
            heating_response,
            humidity_rate,
            sensor_stale,
            calendar_policy=calendar_policy,
        )

    def _calendar_or_limit_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
        calendar_policy: ZoneCalendarPolicy | None,
    ) -> ZoneDecision | None:
        if calendar_policy is None:
            return None
        if calendar_policy.trigger_action != "none":
            return self._decision(
                snapshot,
                runtime,
                calendar_policy.calendar_state,
                calendar_policy.trigger_action,
                calendar_policy.trigger_target_temperature_c,
                calendar_policy.reason,
                room_rate,
                heating_response,
                humidity_rate,
                sensor_stale,
                calendar_policy=calendar_policy,
            )
        if calendar_policy.suppressed_by_renovation:
            return None
        if snapshot.zone.is_guest_zone:
            guest_limit = self._guest_limit_decision(
                snapshot, runtime, room_rate, heating_response, humidity_rate, sensor_stale, calendar_policy
            )
            if guest_limit is not None:
                return guest_limit
        if snapshot.zone.is_service_bathroom_zone or snapshot.zone.is_service_kitchen_zone:
            service_revert = self._service_revert_decision(
                snapshot, runtime, room_rate, heating_response, humidity_rate, sensor_stale, calendar_policy
            )
            if service_revert is not None:
                return service_revert
        return None

    def _guest_limit_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
        calendar_policy: ZoneCalendarPolicy,
    ) -> ZoneDecision | None:
        target = snapshot.target_temperature_c
        if target is None:
            return None
        above_maximum = target > self.settings.guest_max_target_c
        if above_maximum:
            action = "would_enforce_guest_max_target"
            corrected = self.settings.guest_max_target_c
        elif target < self.settings.guest_min_target_c:
            action = "would_enforce_guest_min_target"
            corrected = self.settings.guest_min_target_c
        else:
            return None

        def target_remains_out_of_range(sample: Sample) -> bool:
            sample_target = sample.target_temperature_c
            if sample_target is None:
                return False
            if above_maximum:
                return sample_target > self.settings.guest_max_target_c
            return sample_target < self.settings.guest_min_target_c

        since = self._sample_condition_since(runtime, target_remains_out_of_range)
        if since is None:
            return None
        if snapshot.now - since < timedelta(minutes=self.settings.guest_limit_delay_minutes):
            return None
        return self._decision(
            snapshot,
            runtime,
            "guest_limit",
            action,
            corrected,
            (
                f"guest room target {target:.1f}C has been outside "
                f"{self.settings.guest_min_target_c:.0f}-"
                f"{self.settings.guest_max_target_c:.0f}C for at least "
                f"{self.settings.guest_limit_delay_minutes}m"
            ),
            room_rate,
            heating_response,
            humidity_rate,
            sensor_stale,
            calendar_policy=calendar_policy,
        )

    def _service_revert_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
        calendar_policy: ZoneCalendarPolicy,
    ) -> ZoneDecision | None:
        target = snapshot.target_temperature_c
        default = calendar_policy.baseline_target_temperature_c
        if target is None or default is None or abs(target - default) <= 0.05:
            return None
        since = self._sample_condition_since(
            runtime,
            lambda sample: (
                sample.target_temperature_c is not None
                and abs(sample.target_temperature_c - default) > 0.05
            ),
        )
        if since is None:
            return None
        if snapshot.now - since < timedelta(minutes=self.settings.service_revert_delay_minutes):
            return None
        return self._decision(
            snapshot,
            runtime,
            "service_default_revert",
            "would_restore_service_default",
            default,
            (
                f"service TRV target {target:.1f}C has differed from "
                f"{default:.1f}C for at least {self.settings.service_revert_delay_minutes}m"
            ),
            room_rate,
            heating_response,
            humidity_rate,
            sensor_stale,
            calendar_policy=calendar_policy,
        )

    def _sample_condition_since(
        self, runtime: ZoneRuntime, predicate: Any
    ) -> datetime | None:
        if not runtime.samples or not predicate(runtime.samples[-1]):
            return None
        first = runtime.samples[-1]
        for sample in reversed(runtime.samples):
            if not predicate(sample):
                break
            first = sample
        return first.ts

    def _decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        mode: str,
        suggested_action: str,
        suggested_target_temperature_c: float | None,
        reason: str,
        room_rate: float | None,
        heating_response: float | None,
        humidity_rate: float | None,
        sensor_stale: bool,
        *,
        window_open_risk: bool = False,
        heating_ineffective: bool = False,
        calendar_policy: ZoneCalendarPolicy | None = None,
    ) -> ZoneDecision:
        runtime.last_mode = mode
        runtime.last_reason = reason
        room_age = self._sample_age_minutes(snapshot.now, snapshot.room_sample_ts)
        humidity_age = self._sample_age_minutes(snapshot.now, snapshot.humidity_sample_ts)
        child_lock_age = self._sample_age_seconds(
            snapshot.now, snapshot.child_lock_sample_ts
        )
        return ZoneDecision(
            zone_id=snapshot.zone.zone_id,
            mode=mode,
            suggested_action=suggested_action,
            suggested_target_temperature_c=suggested_target_temperature_c,
            reason=reason,
            room_temperature_c=snapshot.room_temperature_c,
            trv_current_temperature_c=snapshot.trv_current_temperature_c,
            target_temperature_c=snapshot.target_temperature_c,
            hvac_mode=snapshot.hvac_mode,
            hvac_action=snapshot.hvac_action,
            child_lock_on=snapshot.child_lock_on,
            boiler_on=snapshot.boiler_on,
            boiler_available=snapshot.boiler_available,
            room_temperature_rate_c_per_hour=room_rate,
            heating_response_c=heating_response,
            window_open_risk=window_open_risk,
            heating_ineffective=heating_ineffective,
            absolute_humidity_gm3=snapshot.absolute_humidity_gm3,
            relative_humidity_percent=snapshot.relative_humidity_percent,
            absolute_humidity_rate_gm3_per_min=humidity_rate,
            sensor_stale=sensor_stale,
            active_control=self.settings.active_control,
            climate_available=snapshot.climate_available,
            room_sample_age_minutes=room_age,
            humidity_sample_age_minutes=humidity_age,
            child_lock_age_seconds=child_lock_age,
            calendar_policy_state=(
                calendar_policy.calendar_state if calendar_policy is not None else None
            ),
            calendar_policy_target_temperature_c=(
                calendar_policy.baseline_target_temperature_c
                if calendar_policy is not None
                else None
            ),
            calendar_policy_action=(
                calendar_policy.trigger_action if calendar_policy is not None else None
            ),
            calendar_policy_reason=(
                calendar_policy.reason if calendar_policy is not None else None
            ),
            calendar_policy_active_booking=(
                calendar_policy.active_booking if calendar_policy is not None else None
            ),
            calendar_policy_entity_id=(
                calendar_policy.calendar_entity_id if calendar_policy is not None else None
            ),
            calendar_policy_event_summary=(
                calendar_policy.event_summary if calendar_policy is not None else None
            ),
            calendar_policy_suppressed_by_renovation=(
                calendar_policy.suppressed_by_renovation
                if calendar_policy is not None
                else None
            ),
        )

    async def _publish_diagnostics(
        self,
        ha: HomeAssistantClient,
        snapshot: ZoneSnapshot,
        decision: ZoneDecision,
    ) -> None:
        prefix = f"sensor.{self.settings.house_code}_{snapshot.zone.zone_id}_trv_regulator"
        attrs = {
            "friendly_name": f"{snapshot.zone.display_name} TRV Regulator",
            "active_control": self.settings.active_control,
            "climate_entity": snapshot.zone.climate_entity,
            "room_temperature_entity": snapshot.zone.room_temperature_entity,
            "absolute_humidity_entity": snapshot.zone.absolute_humidity_entity,
            "relative_humidity_entity": snapshot.zone.relative_humidity_entity,
            "boiler_entity": self.settings.boiler_entity,
            "boiler_available": decision.boiler_available,
            "reason": decision.reason,
            "hvac_mode": decision.hvac_mode,
            "child_lock_on": decision.child_lock_on,
            "child_lock_age_seconds": decision.child_lock_age_seconds,
            "sensor_stale": decision.sensor_stale,
            "climate_available": decision.climate_available,
            "room_sample_age_minutes": decision.room_sample_age_minutes,
            "humidity_sample_age_minutes": decision.humidity_sample_age_minutes,
            "calendar_policy_state": decision.calendar_policy_state,
            "calendar_policy_target_temperature_c": decision.calendar_policy_target_temperature_c,
            "calendar_policy_action": decision.calendar_policy_action,
            "calendar_policy_reason": decision.calendar_policy_reason,
            "calendar_policy_active_booking": decision.calendar_policy_active_booking,
            "calendar_policy_entity_id": decision.calendar_policy_entity_id,
            "calendar_policy_event_summary": decision.calendar_policy_event_summary,
            "calendar_policy_suppressed_by_renovation": (
                decision.calendar_policy_suppressed_by_renovation
            ),
        }
        await ha.set_state(f"{prefix}_mode", decision.mode, {**attrs, "icon": "mdi:radiator"})
        await ha.set_state(
            f"{prefix}_suggested_action",
            decision.suggested_action,
            {**attrs, "icon": "mdi:comment-processing-outline"},
        )
        await ha.set_state(
            f"{prefix}_reason",
            decision.reason,
            {**attrs, "icon": "mdi:text-box-search-outline"},
        )
        await ha.set_state(
            f"{prefix}_suggested_target",
            self._round_or_unknown(decision.suggested_target_temperature_c, 1),
            {**attrs, "unit_of_measurement": "°C", "icon": "mdi:thermometer-lines"},
        )
        await ha.set_state(
            f"{prefix}_observed_target",
            self._round_or_unknown(decision.target_temperature_c, 1),
            {**attrs, "unit_of_measurement": "°C", "icon": "mdi:thermostat"},
        )
        await ha.set_state(
            f"{prefix}_room_temperature",
            self._round_or_unknown(decision.room_temperature_c, 1),
            {**attrs, "unit_of_measurement": "°C", "icon": "mdi:home-thermometer"},
        )
        await ha.set_state(
            f"{prefix}_boiler_on",
            "on" if decision.boiler_on else "off",
            {**attrs, "icon": "mdi:water-boiler"},
        )
        await ha.set_state(
            f"{prefix}_hvac_mode",
            decision.hvac_mode or "unknown",
            {**attrs, "icon": "mdi:radiator"},
        )
        await ha.set_state(
            f"{prefix}_child_lock_on",
            "unknown"
            if decision.child_lock_on is None
            else ("on" if decision.child_lock_on else "off"),
            {**attrs, "icon": "mdi:lock"},
        )
        await ha.set_state(
            f"{prefix}_calendar_policy",
            decision.calendar_policy_state or "unknown",
            {**attrs, "icon": "mdi:calendar-clock"},
        )
        await ha.set_state(
            f"{prefix}_calendar_policy_target",
            self._round_or_unknown(decision.calendar_policy_target_temperature_c, 1),
            {
                **attrs,
                "unit_of_measurement": "°C",
                "icon": "mdi:calendar-thermometer",
            },
        )
        await ha.set_state(
            f"{prefix}_room_temperature_rate",
            self._round_or_unknown(decision.room_temperature_rate_c_per_hour, 3),
            {**attrs, "unit_of_measurement": "°C/h", "icon": "mdi:chart-line"},
        )
        await ha.set_state(
            f"{prefix}_heating_response",
            self._round_or_unknown(decision.heating_response_c, 2),
            {
                **attrs,
                "unit_of_measurement": "°C",
                "icon": "mdi:radiator-disabled",
            },
        )
        await ha.set_state(
            f"{prefix}_window_open_risk",
            "on" if decision.window_open_risk else "off",
            {**attrs, "icon": "mdi:window-open-variant"},
        )
        await ha.set_state(
            f"{prefix}_drying_absolute_humidity_rate",
            self._round_or_unknown(decision.absolute_humidity_rate_gm3_per_min, 3),
            {
                **attrs,
                "unit_of_measurement": "g/m3/min",
                "icon": "mdi:water-sync",
            },
        )
        await ha.set_state(
            f"{prefix}_climate_available",
            "on" if decision.climate_available else "off",
            {**attrs, "icon": "mdi:thermostat"},
        )
        await ha.set_state(
            f"{prefix}_sensor_stale",
            "on" if decision.sensor_stale else "off",
            {**attrs, "icon": "mdi:clock-alert-outline"},
        )
        await ha.set_state(
            f"{prefix}_heating_ineffective",
            "on" if decision.heating_ineffective else "off",
            {**attrs, "icon": "mdi:radiator-off"},
        )

    def _boiler_decision(
        self,
        decisions: list[ZoneDecision],
        *,
        boiler_on: bool,
        boiler_available: bool,
    ) -> BoilerDecision:
        demanding = tuple(
            decision.zone_id
            for decision in decisions
            if decision.climate_available and decision.hvac_action == "heating"
        )
        unavailable = tuple(
            decision.zone_id
            for decision in decisions
            if not decision.climate_available
            or decision.hvac_action not in {"heating", "idle", "off"}
        )
        should_be_on = bool(demanding)
        control_safe = boiler_available and (should_be_on or not unavailable)
        mode = "boiler_matched"
        action = "none"
        reason = "boiler matches aggregate TRV demand"

        if not boiler_available:
            mode = "boiler_unavailable"
            action = "blocked_boiler_unavailable"
            reason = "boiler relay unavailable; no command is safe"
        elif should_be_on and not boiler_on:
            mode = "boiler_demand_mismatch"
            action = "would_turn_boiler_on"
            reason = "heating demand from " + ", ".join(demanding)
        elif not should_be_on and boiler_on and unavailable:
            mode = "boiler_demand_unknown"
            action = "blocked_turn_off_trv_unavailable"
            reason = (
                "boiler is on with no known demand, but unavailable TRVs prevent "
                "a safe turn-off: " + ", ".join(unavailable)
            )
        elif not should_be_on and boiler_on:
            mode = "boiler_demand_mismatch"
            action = "would_turn_boiler_off"
            reason = "all configured TRVs are available and none demand heat"
        elif not should_be_on and unavailable:
            mode = "boiler_demand_unknown"
            action = "none"
            reason = "no known heat demand; unavailable TRVs: " + ", ".join(unavailable)

        return BoilerDecision(
            zone_id="boiler",
            mode=mode,
            suggested_action=action,
            reason=reason,
            boiler_entity=self.settings.boiler_entity,
            boiler_on=boiler_on,
            boiler_available=boiler_available,
            boiler_should_be_on=should_be_on,
            demanding_zone_ids=demanding,
            unavailable_zone_ids=unavailable,
            control_safe=control_safe,
            state_mismatch=action in {"would_turn_boiler_on", "would_turn_boiler_off"},
            active_control=(
                self.settings.active_control and self.settings.active_boiler_control
            ),
        )

    async def _publish_boiler_diagnostics(
        self, ha: HomeAssistantClient, decision: BoilerDecision
    ) -> None:
        prefix = f"sensor.{self.settings.house_code}_trv_regulator_boiler"
        attrs = {
            "friendly_name": f"{self.settings.house_code} TRV Regulator Boiler",
            "boiler_entity": decision.boiler_entity,
            "boiler_on": decision.boiler_on,
            "boiler_available": decision.boiler_available,
            "boiler_should_be_on": decision.boiler_should_be_on,
            "demanding_zone_ids": list(decision.demanding_zone_ids),
            "unavailable_zone_ids": list(decision.unavailable_zone_ids),
            "control_safe": decision.control_safe,
            "state_mismatch": decision.state_mismatch,
            "active_control": decision.active_control,
            "suggested_action": decision.suggested_action,
            "reason": decision.reason,
        }
        await ha.set_state(
            f"{prefix}_policy", decision.mode, {**attrs, "icon": "mdi:water-boiler-alert"}
        )
        await ha.set_state(
            f"{prefix}_available",
            "on" if decision.boiler_available else "off",
            {**attrs, "icon": "mdi:access-point-check"},
        )
        await ha.set_state(
            f"{prefix}_should_be_on",
            "on" if decision.boiler_should_be_on else "off",
            {**attrs, "icon": "mdi:radiator"},
        )
        await ha.set_state(
            f"{prefix}_mismatch",
            "on" if decision.state_mismatch else "off",
            {**attrs, "icon": "mdi:alert-circle-outline"},
        )

    async def _apply_boiler_decision(
        self, ha: HomeAssistantClient, decision: BoilerDecision
    ) -> None:
        if not (self.settings.active_control and self.settings.active_boiler_control):
            return
        if not decision.control_safe:
            LOGGER.error("Refusing unsafe boiler command: %s", decision.reason)
            return
        if decision.suggested_action == "would_turn_boiler_on":
            await ha.set_switch_state_verified(decision.boiler_entity, on=True)
        elif decision.suggested_action == "would_turn_boiler_off":
            await ha.set_switch_state_verified(decision.boiler_entity, on=False)

    async def _publish_health(
        self,
        ha: HomeAssistantClient,
        decisions: list[ZoneDecision],
        boiler: BoilerDecision,
    ) -> None:
        unavailable = [
            decision.zone_id
            for decision in decisions
            if not decision.climate_available
            or decision.hvac_action not in {"heating", "idle", "off"}
        ]
        stale = [decision.zone_id for decision in decisions if decision.sensor_stale]
        if self.control_errors:
            status = "control_error"
        elif not boiler.control_safe or unavailable or stale:
            status = "blocked"
        else:
            status = "ok"
        await ha.set_state(
            f"sensor.{self.settings.house_code}_trv_regulator_health",
            status,
            {
                "friendly_name": f"{self.settings.house_code} TRV Regulator Health",
                "active_control": self.settings.active_control,
                "active_boiler_control": self.settings.active_boiler_control,
                "active_calendar_policy": self.settings.active_calendar_policy,
                "boiler_available": boiler.boiler_available,
                "boiler_control_safe": boiler.control_safe,
                "boiler_policy_action": boiler.suggested_action,
                "trv_unavailable_or_unknown_demand": unavailable,
                "sensor_stale": stale,
                "control_errors": dict(sorted(self.control_errors.items())),
                "icon": "mdi:radiator-off" if status != "ok" else "mdi:radiator",
            },
        )

    async def _apply_decision(
        self,
        ha: HomeAssistantClient,
        snapshot: ZoneSnapshot,
        decision: ZoneDecision,
    ) -> None:
        if not self.settings.active_control:
            return
        if not snapshot.climate_available:
            LOGGER.error(
                "Refusing to control unavailable TRV %s", snapshot.zone.climate_entity
            )
            return
        if (
            decision.suggested_action == "would_raise_drying_target"
            and decision.suggested_target_temperature_c is not None
            and snapshot.target_temperature_c is not None
            and decision.suggested_target_temperature_c > snapshot.target_temperature_c
        ):
            if decision.sensor_stale:
                LOGGER.error(
                    "Refusing drying target write with stale sensor for %s",
                    snapshot.zone.climate_entity,
                )
                return
            LOGGER.info(
                "Setting %s to %.1fC: %s",
                snapshot.zone.climate_entity,
                decision.suggested_target_temperature_c,
                decision.reason,
            )
            await ha.set_climate_temperature_verified(
                snapshot.zone.climate_entity, decision.suggested_target_temperature_c
            )
            return
        if not self.settings.active_calendar_policy:
            return
        if decision.suggested_action in {
            "would_set_calendar_checkin_target",
            "would_set_calendar_checkout_target",
            "would_enforce_guest_max_target",
            "would_enforce_guest_min_target",
            "would_restore_service_default",
        }:
            if decision.suggested_target_temperature_c is None:
                return
            LOGGER.info(
                "Applying calendar policy to %s: %.1fC (%s)",
                snapshot.zone.climate_entity,
                decision.suggested_target_temperature_c,
                decision.reason,
            )
            await ha.set_climate_temperature_verified(
                snapshot.zone.climate_entity, decision.suggested_target_temperature_c
            )
            return
        if decision.suggested_action == "would_set_hvac_mode_heat":
            LOGGER.info("Restoring %s HVAC mode to heat", snapshot.zone.climate_entity)
            await ha.set_climate_hvac_mode_verified(snapshot.zone.climate_entity, "heat")
            return
        if (
            decision.suggested_action == "would_restore_child_lock"
            and snapshot.zone.child_lock_entity
        ):
            LOGGER.info("Restoring child lock for %s", snapshot.zone.climate_entity)
            await ha.set_switch_state_verified(snapshot.zone.child_lock_entity, on=True)

    def dashboard_payload(self) -> dict[str, Any]:
        zones = []
        for decision in self.last_decisions:
            zones.append({
                "zone_id": decision.zone_id,
                "mode": decision.mode,
                "suggested_action": decision.suggested_action,
                "suggested_target_temperature_c": decision.suggested_target_temperature_c,
                "reason": decision.reason,
                "room_temperature_c": decision.room_temperature_c,
                "trv_current_temperature_c": decision.trv_current_temperature_c,
                "target_temperature_c": decision.target_temperature_c,
                "hvac_mode": decision.hvac_mode,
                "hvac_action": decision.hvac_action,
                "child_lock_on": decision.child_lock_on,
                "child_lock_age_seconds": decision.child_lock_age_seconds,
                "boiler_on": decision.boiler_on,
                "boiler_available": decision.boiler_available,
                "room_temperature_rate_c_per_hour": decision.room_temperature_rate_c_per_hour,
                "heating_response_c": decision.heating_response_c,
                "window_open_risk": decision.window_open_risk,
                "heating_ineffective": decision.heating_ineffective,
                "absolute_humidity_gm3": decision.absolute_humidity_gm3,
                "relative_humidity_percent": decision.relative_humidity_percent,
                "absolute_humidity_rate_gm3_per_min": decision.absolute_humidity_rate_gm3_per_min,
                "sensor_stale": decision.sensor_stale,
                "active_control": decision.active_control,
                "climate_available": decision.climate_available,
                "room_sample_age_minutes": decision.room_sample_age_minutes,
                "humidity_sample_age_minutes": decision.humidity_sample_age_minutes,
                "calendar_policy_state": decision.calendar_policy_state,
                "calendar_policy_target_temperature_c": decision.calendar_policy_target_temperature_c,
                "calendar_policy_action": decision.calendar_policy_action,
                "calendar_policy_reason": decision.calendar_policy_reason,
                "calendar_policy_active_booking": decision.calendar_policy_active_booking,
                "calendar_policy_entity_id": decision.calendar_policy_entity_id,
                "calendar_policy_event_summary": decision.calendar_policy_event_summary,
                "calendar_policy_suppressed_by_renovation": (
                    decision.calendar_policy_suppressed_by_renovation
                ),
            })
        boiler = self.last_boiler_decision
        unavailable = [zone["zone_id"] for zone in zones if not zone["climate_available"]]
        stale = [zone["zone_id"] for zone in zones if zone["sensor_stale"]]
        return {
            "app": "trv_regulator",
            "house_code": self.settings.house_code,
            "active_control": self.settings.active_control,
            "active_boiler_control": self.settings.active_boiler_control,
            "calendar_policy_enabled": self.settings.calendar_policy_enabled,
            "active_calendar_policy": self.settings.active_calendar_policy,
            "boiler_entity": self.settings.boiler_entity,
            "boiler_on": boiler.boiler_on if boiler else False,
            "boiler_available": boiler.boiler_available if boiler else False,
            "boiler_should_be_on": boiler.boiler_should_be_on if boiler else False,
            "boiler_policy_action": boiler.suggested_action if boiler else "unknown",
            "boiler_policy": None if boiler is None else {
                "mode": boiler.mode,
                "reason": boiler.reason,
                "control_safe": boiler.control_safe,
                "state_mismatch": boiler.state_mismatch,
                "demanding_zone_ids": list(boiler.demanding_zone_ids),
                "unavailable_zone_ids": list(boiler.unavailable_zone_ids),
            },
            "active_control_ready_now": bool(
                boiler
                and boiler.control_safe
                and not unavailable
                and not stale
                and not self.control_errors
            ),
            "readiness_blockers": {
                "boiler_unavailable": bool(boiler and not boiler.boiler_available),
                "trv_unavailable": unavailable,
                "sensor_stale": stale,
                "control_errors": dict(sorted(self.control_errors.items())),
                "cutover_requirements": [
                    "disable legacy policy automations only when the matching write gate is enabled",
                    "keep renovation-mode restore and drying-room daily reset in Home Assistant until modeled",
                ],
            },
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "zones": zones,
            "recent_events": self.store.recent_events(),
            "daily_summaries": self.store.daily_summaries(),
        }

    def _sample_age_minutes(self, now: datetime, sample_ts: datetime | None) -> float | None:
        if sample_ts is None:
            return None
        return (now - sample_ts).total_seconds() / 60

    def _sample_age_seconds(self, now: datetime, sample_ts: datetime | None) -> float | None:
        if sample_ts is None:
            return None
        return (now - sample_ts).total_seconds()

    def _format_rate(self, rate: float | None) -> str:
        return "unknown" if rate is None else f"{rate:.3f} g/m3/min"

    def _round_or_unknown(self, value: float | None, digits: int = 2) -> str | float:
        return "unknown" if value is None else round(value, digits)
