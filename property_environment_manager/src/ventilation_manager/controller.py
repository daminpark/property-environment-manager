"""Ventilation control state machine."""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ventilation_manager.config import Settings, ZoneConfig
from ventilation_manager.event_store import EventStore
from ventilation_manager.humidity import absolute_humidity_gm3

if TYPE_CHECKING:
    from ventilation_manager.ha.client import EntityState, HomeAssistantClient

LOGGER = logging.getLogger(__name__)


def parse_ha_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def as_float(state: Any | None) -> float | None:
    if state is None or state.state in {"unknown", "unavailable", "none", ""}:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


@dataclass
class Sample:
    """One humidity sample."""

    ts: datetime
    absolute_humidity: float
    relative_humidity: float

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "absolute_humidity": self.absolute_humidity,
            "relative_humidity": self.relative_humidity,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Sample":
        return cls(
            ts=datetime.fromisoformat(data["ts"]).astimezone(UTC),
            absolute_humidity=float(data["absolute_humidity"]),
            relative_humidity=float(data["relative_humidity"]),
        )


@dataclass
class ZoneRuntime:
    """Persistent runtime state for one zone."""

    zone_id: str
    mode: str = "idle"
    baseline_absolute_humidity: float | None = None
    event_baseline_absolute_humidity: float | None = None
    peak_absolute_humidity: float | None = None
    humidity_event_started_at: datetime | None = None
    fan_commanded_on_at: datetime | None = None
    last_sample: Sample | None = None
    stable_samples: list[Sample] = field(default_factory=list)
    last_command: str = "none"
    last_reason: str = "starting"

    def to_json(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "mode": self.mode,
            "baseline_absolute_humidity": self.baseline_absolute_humidity,
            "event_baseline_absolute_humidity": self.event_baseline_absolute_humidity,
            "peak_absolute_humidity": self.peak_absolute_humidity,
            "humidity_event_started_at": (
                self.humidity_event_started_at.isoformat()
                if self.humidity_event_started_at
                else None
            ),
            "fan_commanded_on_at": (
                self.fan_commanded_on_at.isoformat() if self.fan_commanded_on_at else None
            ),
            "last_sample": self.last_sample.to_json() if self.last_sample else None,
            "stable_samples": [sample.to_json() for sample in self.stable_samples],
            "last_command": self.last_command,
            "last_reason": self.last_reason,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ZoneRuntime":
        runtime = cls(zone_id=str(data["zone_id"]))
        runtime.mode = str(data.get("mode", "idle"))
        runtime.baseline_absolute_humidity = data.get("baseline_absolute_humidity")
        runtime.event_baseline_absolute_humidity = data.get(
            "event_baseline_absolute_humidity"
        )
        runtime.peak_absolute_humidity = data.get("peak_absolute_humidity")
        if data.get("humidity_event_started_at"):
            runtime.humidity_event_started_at = datetime.fromisoformat(
                data["humidity_event_started_at"]
            ).astimezone(UTC)
        if data.get("fan_commanded_on_at"):
            runtime.fan_commanded_on_at = datetime.fromisoformat(
                data["fan_commanded_on_at"]
            ).astimezone(UTC)
        if data.get("last_sample"):
            runtime.last_sample = Sample.from_json(data["last_sample"])
        runtime.stable_samples = [
            Sample.from_json(sample) for sample in data.get("stable_samples", [])
        ]
        runtime.last_command = str(data.get("last_command", "none"))
        runtime.last_reason = str(data.get("last_reason", "restored"))
        return runtime


@dataclass(frozen=True)
class ZoneSnapshot:
    """Current HA-derived state for one zone."""

    zone: ZoneConfig
    now: datetime
    fan_on: bool
    fan_available: bool
    relative_humidity: float | None
    temperature_c: float | None
    absolute_humidity: float | None
    sample_ts: datetime | None

    @property
    def is_complete(self) -> bool:
        return (
            self.relative_humidity is not None
            and self.absolute_humidity is not None
            and self.sample_ts is not None
        )


@dataclass(frozen=True)
class ZoneDecision:
    """Decision and diagnostics for one zone."""

    zone_id: str
    mode: str
    should_run: bool
    command: str
    reason: str
    baseline_absolute_humidity: float | None
    event_baseline_absolute_humidity: float | None
    absolute_humidity: float | None
    relative_humidity: float | None
    delta_absolute_humidity: float | None
    rate_gm3_per_min: float | None
    sensor_stale: bool
    sample_age_minutes: float | None
    fan_on: bool
    fan_available: bool
    fan_state_mismatch: bool
    write_blocked: bool


class VentilationController:
    """Stateful humidity controller."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtimes: dict[str, ZoneRuntime] = {
            zone.zone_id: ZoneRuntime(zone_id=zone.zone_id) for zone in settings.zones
        }
        self.last_decisions: list[ZoneDecision] = []
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
        decisions: list[ZoneDecision] = []
        for zone in self.settings.zones:
            snapshot = self._snapshot(zone, states, now)
            decision = self.evaluate(snapshot)
            decisions.append(decision)
            await self._publish_diagnostics(ha, snapshot, decision)
            try:
                await self._apply_decision(ha, snapshot, decision)
                self.control_errors.pop(zone.zone_id, None)
            except Exception as exc:
                self.control_errors[zone.zone_id] = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("Fan command failed for zone %s", zone.zone_id)
        await self._publish_health(ha, decisions)
        self.last_decisions = decisions
        self.last_run_at = now
        self.save_state()
        self.store.record_run(decisions, run_at=now)
        return decisions

    def evaluate(self, snapshot: ZoneSnapshot) -> ZoneDecision:
        runtime = self.runtimes.setdefault(
            snapshot.zone.zone_id, ZoneRuntime(zone_id=snapshot.zone.zone_id)
        )

        if not snapshot.is_complete:
            runtime.mode = "sensor_unavailable"
            runtime.last_command = "none"
            runtime.last_reason = "missing humidity, temperature, or sample timestamp"
            return self._decision(snapshot, runtime, False, "none", runtime.last_reason)

        assert snapshot.absolute_humidity is not None
        assert snapshot.relative_humidity is not None
        assert snapshot.sample_ts is not None

        sample_age = snapshot.now - snapshot.sample_ts
        sensor_stale = sample_age > timedelta(minutes=self.settings.sensor_stale_minutes)
        sample = Sample(
            ts=snapshot.sample_ts,
            absolute_humidity=snapshot.absolute_humidity,
            relative_humidity=snapshot.relative_humidity,
        )
        rate = self._rate(runtime.last_sample, sample)

        if runtime.last_sample is None or runtime.last_sample.ts != sample.ts:
            runtime.last_sample = sample

        baseline = self._baseline(runtime, sample, rate, snapshot.fan_on)
        delta = (
            None
            if baseline is None
            else snapshot.absolute_humidity - baseline
        )

        if sensor_stale:
            return self._stale_sensor_decision(
                snapshot, runtime, rate, sample_age, baseline, delta
            )

        if baseline is None:
            return self._no_baseline_decision(snapshot, runtime, rate)

        moisture_rising = self._moisture_rising(delta, rate, snapshot.relative_humidity)
        active_event = runtime.mode in {"drying", "moisture_rising"}

        if not active_event and moisture_rising:
            runtime.mode = "moisture_rising"
            runtime.event_baseline_absolute_humidity = baseline
            runtime.peak_absolute_humidity = snapshot.absolute_humidity
            runtime.humidity_event_started_at = snapshot.now
            # This timestamp represents the counterfactual controller runtime. It
            # must advance in observer mode even when the legacy automation leaves
            # the physical fan off.
            runtime.fan_commanded_on_at = snapshot.now
            reason = self._start_reason(delta, rate, snapshot.relative_humidity)
            runtime.last_reason = reason
            return self._decision(snapshot, runtime, True, "turn_on", reason, rate)

        if active_event:
            if (
                runtime.peak_absolute_humidity is None
                or snapshot.absolute_humidity > runtime.peak_absolute_humidity
            ):
                runtime.peak_absolute_humidity = snapshot.absolute_humidity

            event_baseline = runtime.event_baseline_absolute_humidity or baseline
            dry_target = (
                None
                if event_baseline is None
                else event_baseline + self.settings.baseline_margin_gm3
            )
            minimum_elapsed = self._minimum_runtime_elapsed(runtime, snapshot)
            still_too_humid = (
                snapshot.relative_humidity >= self.settings.high_rh_guard_percent
            )
            near_baseline = dry_target is not None and snapshot.absolute_humidity <= dry_target
            stable_or_falling = rate is None or rate <= self.settings.stable_rate_gm3_per_min

            if (
                near_baseline
                and stable_or_falling
                and not still_too_humid
                and minimum_elapsed
            ):
                runtime.mode = "idle"
                runtime.event_baseline_absolute_humidity = None
                runtime.peak_absolute_humidity = None
                runtime.humidity_event_started_at = None
                runtime.last_reason = "back near baseline and stable"
                return self._decision(
                    snapshot, runtime, False, "turn_off", runtime.last_reason, rate
                )

            runtime.mode = "drying"
            runtime.last_reason = self._continue_reason(
                dry_target, snapshot.absolute_humidity, snapshot.relative_humidity, rate
            )
            return self._decision(
                snapshot, runtime, True, "keep_on", runtime.last_reason, rate
            )

        runtime.mode = "idle"
        runtime.event_baseline_absolute_humidity = None
        runtime.peak_absolute_humidity = None
        runtime.humidity_event_started_at = None
        runtime.fan_commanded_on_at = None
        runtime.last_reason = "idle; no moisture event"
        return self._decision(snapshot, runtime, False, "none", runtime.last_reason, rate)

    def _snapshot(
        self,
        zone: ZoneConfig,
        states: dict[str, EntityState],
        now: datetime,
    ) -> ZoneSnapshot:
        fan = states.get(zone.fan_entity)
        rh = states.get(zone.humidity_entity)
        temp = states.get(zone.temperature_entity)
        abs_humidity = states.get(zone.absolute_humidity_entity)

        relative_humidity = as_float(rh)
        temperature_c = as_float(temp)
        absolute_humidity = as_float(abs_humidity)
        if absolute_humidity is None and relative_humidity is not None and temperature_c is not None:
            absolute_humidity = round(
                absolute_humidity_gm3(relative_humidity, temperature_c), 2
            )

        humidity_ts = parse_ha_timestamp(rh.last_updated if rh else None)
        if zone.change_only_sensor:
            # A derived absolute-humidity entity can look fresh merely because
            # temperature changed. For sparse sensors, the source RH timestamp
            # is the only honest indication that moisture was sampled again.
            sample_ts = humidity_ts
        else:
            sample_ts = max(
                (
                    ts
                    for ts in (
                        humidity_ts,
                        parse_ha_timestamp(temp.last_updated if temp else None),
                        parse_ha_timestamp(
                            abs_humidity.last_updated if abs_humidity else None
                        ),
                    )
                    if ts is not None
                ),
                default=None,
            )

        return ZoneSnapshot(
            zone=zone,
            now=now,
            fan_on=fan is not None and fan.state == "on",
            fan_available=fan is not None and fan.state in {"on", "off"},
            relative_humidity=relative_humidity,
            temperature_c=temperature_c,
            absolute_humidity=absolute_humidity,
            sample_ts=sample_ts,
        )

    def _baseline(
        self,
        runtime: ZoneRuntime,
        sample: Sample,
        rate: float | None,
        fan_on: bool,
    ) -> float | None:
        cutoff = sample.ts - timedelta(minutes=self.settings.baseline_window_minutes)
        runtime.stable_samples = [
            stable for stable in runtime.stable_samples if stable.ts >= cutoff
        ]

        stable_rate = rate is None or abs(rate) <= self.settings.stable_rate_gm3_per_min
        if (
            runtime.mode == "idle"
            and not fan_on
            and stable_rate
            and sample.relative_humidity < self.settings.high_rh_guard_percent
            and not any(existing.ts == sample.ts for existing in runtime.stable_samples)
        ):
            runtime.stable_samples.append(sample)

        if runtime.stable_samples:
            runtime.baseline_absolute_humidity = statistics.median(
                stable.absolute_humidity for stable in runtime.stable_samples
            )
        elif (
            runtime.baseline_absolute_humidity is None
            and not fan_on
            and sample.relative_humidity < self.settings.high_rh_guard_percent
        ):
            runtime.baseline_absolute_humidity = sample.absolute_humidity

        return runtime.baseline_absolute_humidity

    def _moisture_rising(
        self,
        delta: float | None,
        rate: float | None,
        relative_humidity: float,
    ) -> bool:
        delta_trigger = (
            delta is not None and delta >= self.settings.rise_delta_threshold_gm3
        )
        rate_trigger = (
            rate is not None
            and rate >= self.settings.rise_rate_threshold_gm3_per_min
            and delta is not None
            and delta >= self.settings.baseline_margin_gm3
        )
        high_rh_trigger = (
            delta is not None
            and relative_humidity >= self.settings.high_rh_guard_percent
            and delta >= self.settings.baseline_margin_gm3
        )
        return delta_trigger or rate_trigger or high_rh_trigger

    def _minimum_runtime_elapsed(self, runtime: ZoneRuntime, snapshot: ZoneSnapshot) -> bool:
        started_at = runtime.fan_commanded_on_at or runtime.humidity_event_started_at
        return started_at is None or snapshot.now - started_at >= timedelta(
            minutes=self.settings.min_runtime_minutes
        )

    def _stale_sensor_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        rate: float | None,
        sample_age: timedelta,
        baseline: float | None,
        delta: float | None,
    ) -> ZoneDecision:
        runtime.mode = "sensor_stale"
        active_event = runtime.humidity_event_started_at is not None
        high_humidity = (
            snapshot.relative_humidity is not None
            and snapshot.relative_humidity >= self.settings.high_rh_guard_percent
        )
        elevated_from_baseline = (
            baseline is not None
            and delta is not None
            and delta >= self.settings.baseline_margin_gm3
        )
        conservative_run = (
            snapshot.zone.change_only_sensor
            and high_humidity
            and (elevated_from_baseline or active_event or baseline is None)
        )

        if snapshot.fan_on or active_event or conservative_run:
            if runtime.humidity_event_started_at is None:
                runtime.humidity_event_started_at = snapshot.now
                runtime.fan_commanded_on_at = snapshot.now
            runtime.last_reason = "sensor stale; keep fan on conservatively"
            if snapshot.zone.change_only_sensor:
                runtime.last_reason = (
                    "change-only sensor stale with high humidity; keep fan on "
                    "until a fresh lower reading"
                )
            return self._decision(
                snapshot, runtime, True, "keep_on", runtime.last_reason, rate
            )
        runtime.last_reason = (
            f"sensor stale for {sample_age.total_seconds() / 60:.1f} minutes"
        )
        return self._decision(snapshot, runtime, False, "none", runtime.last_reason, rate)

    def _no_baseline_decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        rate: float | None,
    ) -> ZoneDecision:
        runtime.mode = "learning_baseline"
        if (
            snapshot.relative_humidity is not None
            and snapshot.relative_humidity >= self.settings.high_rh_guard_percent
        ):
            if runtime.humidity_event_started_at is None:
                runtime.humidity_event_started_at = snapshot.now
                runtime.fan_commanded_on_at = snapshot.now
            runtime.last_reason = "no safe baseline yet; RH is above high-humidity guard"
            return self._decision(
                snapshot, runtime, True, "keep_on", runtime.last_reason, rate
            )
        runtime.last_reason = "learning baseline from stable humidity"
        return self._decision(snapshot, runtime, False, "none", runtime.last_reason, rate)

    def _rate(self, previous: Sample | None, current: Sample) -> float | None:
        if previous is None or previous.ts == current.ts:
            return None
        minutes = (current.ts - previous.ts).total_seconds() / 60
        if minutes <= 0:
            return None
        return (current.absolute_humidity - previous.absolute_humidity) / minutes

    def _start_reason(
        self, delta: float | None, rate: float | None, relative_humidity: float
    ) -> str:
        parts: list[str] = []
        if delta is not None:
            parts.append(f"delta {delta:.2f} g/m3 above baseline")
        if rate is not None:
            parts.append(f"rate {rate:.3f} g/m3/min")
        parts.append(f"RH {relative_humidity:.1f}%")
        return "moisture event started: " + ", ".join(parts)

    def _continue_reason(
        self,
        dry_target: float | None,
        absolute_humidity: float,
        relative_humidity: float,
        rate: float | None,
    ) -> str:
        target = "unknown" if dry_target is None else f"{dry_target:.2f}"
        rate_text = "unknown" if rate is None else f"{rate:.3f}"
        return (
            f"drying; abs {absolute_humidity:.2f} g/m3, target {target}, "
            f"RH {relative_humidity:.1f}%, rate {rate_text}"
        )

    def _decision(
        self,
        snapshot: ZoneSnapshot,
        runtime: ZoneRuntime,
        should_run: bool,
        command: str,
        reason: str,
        rate: float | None = None,
    ) -> ZoneDecision:
        runtime.last_command = command
        baseline = runtime.baseline_absolute_humidity
        delta = (
            None
            if baseline is None or snapshot.absolute_humidity is None
            else snapshot.absolute_humidity - baseline
        )
        sample_age = (
            None
            if snapshot.sample_ts is None
            else (snapshot.now - snapshot.sample_ts).total_seconds() / 60
        )
        sensor_stale = (
            sample_age is not None
            and sample_age > self.settings.sensor_stale_minutes
        )
        fan_state_mismatch = (
            snapshot.fan_available and should_run != snapshot.fan_on
        )
        return ZoneDecision(
            zone_id=snapshot.zone.zone_id,
            mode=runtime.mode,
            should_run=should_run,
            command=command,
            reason=reason,
            baseline_absolute_humidity=baseline,
            event_baseline_absolute_humidity=runtime.event_baseline_absolute_humidity,
            absolute_humidity=snapshot.absolute_humidity,
            relative_humidity=snapshot.relative_humidity,
            delta_absolute_humidity=delta,
            rate_gm3_per_min=rate,
            sensor_stale=sensor_stale,
            sample_age_minutes=sample_age,
            fan_on=snapshot.fan_on,
            fan_available=snapshot.fan_available,
            fan_state_mismatch=fan_state_mismatch,
            write_blocked=not snapshot.fan_available,
        )

    async def _publish_diagnostics(
        self,
        ha: HomeAssistantClient,
        snapshot: ZoneSnapshot,
        decision: ZoneDecision,
    ) -> None:
        prefix = f"sensor.{self.settings.house_code}_{snapshot.zone.zone_id}_ventilation"
        base_attrs = {
            "friendly_name": f"{snapshot.zone.display_name} Ventilation",
            "fan_entity": snapshot.zone.fan_entity,
            "humidity_entity": snapshot.zone.humidity_entity,
            "temperature_entity": snapshot.zone.temperature_entity,
            "absolute_humidity_entity": snapshot.zone.absolute_humidity_entity,
            "active_control": self.settings.active_control,
            "sample_age_minutes": decision.sample_age_minutes,
            "sensor_stale": decision.sensor_stale,
            "change_only_sensor": snapshot.zone.change_only_sensor,
            "fan_available": decision.fan_available,
            "fan_state_mismatch": decision.fan_state_mismatch,
            "write_blocked": decision.write_blocked,
            "reason": decision.reason,
        }
        await ha.set_state(
            f"{prefix}_mode",
            decision.mode,
            {**base_attrs, "icon": "mdi:fan-auto"},
        )
        await ha.set_state(
            f"{prefix}_should_run",
            "on" if decision.should_run else "off",
            {**base_attrs, "icon": "mdi:fan-alert"},
        )
        await ha.set_state(
            f"{prefix}_reason",
            decision.reason,
            {**base_attrs, "icon": "mdi:text-box-search-outline"},
        )
        await ha.set_state(
            f"{prefix}_fan_mismatch",
            "on" if decision.fan_state_mismatch else "off",
            {**base_attrs, "icon": "mdi:fan-alert"},
        )
        await ha.set_state(
            f"{prefix}_baseline_absolute_humidity",
            self._round_or_unknown(decision.baseline_absolute_humidity),
            {
                **base_attrs,
                "unit_of_measurement": "g/m3",
                "state_class": "measurement",
                "icon": "mdi:water-percent",
            },
        )
        await ha.set_state(
            f"{prefix}_delta_absolute_humidity",
            self._round_or_unknown(decision.delta_absolute_humidity),
            {
                **base_attrs,
                "unit_of_measurement": "g/m3",
                "state_class": "measurement",
                "icon": "mdi:delta",
            },
        )
        await ha.set_state(
            f"{prefix}_rate",
            self._round_or_unknown(decision.rate_gm3_per_min, digits=3),
            {
                **base_attrs,
                "unit_of_measurement": "g/m3/min",
                "state_class": "measurement",
                "icon": "mdi:chart-timeline-variant",
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
        if not snapshot.fan_available:
            LOGGER.error(
                "Refusing to control unavailable fan %s", snapshot.zone.fan_entity
            )
            return
        if decision.should_run and not snapshot.fan_on:
            LOGGER.info("Turning on %s: %s", snapshot.zone.fan_entity, decision.reason)
            await ha.set_switch_state_verified(snapshot.zone.fan_entity, on=True)
        elif not decision.should_run and snapshot.fan_on and decision.command == "turn_off":
            LOGGER.info("Turning off %s: %s", snapshot.zone.fan_entity, decision.reason)
            await ha.set_switch_state_verified(snapshot.zone.fan_entity, on=False)

    async def _publish_health(
        self, ha: HomeAssistantClient, decisions: list[ZoneDecision]
    ) -> None:
        unavailable = [
            decision.zone_id for decision in decisions if not decision.fan_available
        ]
        stale = [decision.zone_id for decision in decisions if decision.sensor_stale]
        if self.control_errors:
            status = "control_error"
        elif unavailable or stale:
            status = "blocked"
        else:
            status = "ok"
        await ha.set_state(
            f"sensor.{self.settings.house_code}_ventilation_manager_health",
            status,
            {
                "friendly_name": (
                    f"{self.settings.house_code} Ventilation Manager Health"
                ),
                "active_control": self.settings.active_control,
                "fan_unavailable": unavailable,
                "sensor_stale": stale,
                "control_errors": dict(sorted(self.control_errors.items())),
                "icon": "mdi:fan-alert" if status != "ok" else "mdi:fan-check",
            },
        )

    def dashboard_payload(self) -> dict[str, Any]:
        zones = []
        for decision in self.last_decisions:
            zones.append({
                "zone_id": decision.zone_id,
                "mode": decision.mode,
                "should_run": decision.should_run,
                "command": decision.command,
                "reason": decision.reason,
                "baseline_absolute_humidity": decision.baseline_absolute_humidity,
                "event_baseline_absolute_humidity": decision.event_baseline_absolute_humidity,
                "absolute_humidity": decision.absolute_humidity,
                "relative_humidity": decision.relative_humidity,
                "delta_absolute_humidity": decision.delta_absolute_humidity,
                "rate_gm3_per_min": decision.rate_gm3_per_min,
                "sensor_stale": decision.sensor_stale,
                "sample_age_minutes": decision.sample_age_minutes,
                "fan_on": decision.fan_on,
                "fan_available": decision.fan_available,
                "fan_state_mismatch": decision.fan_state_mismatch,
                "write_blocked": decision.write_blocked,
            })
        unavailable = [zone["zone_id"] for zone in zones if not zone["fan_available"]]
        stale = [zone["zone_id"] for zone in zones if zone["sensor_stale"]]
        return {
            "app": "ventilation_manager",
            "house_code": self.settings.house_code,
            "active_control": self.settings.active_control,
            "control_scope": "humidity_only",
            "active_control_ready_now": (
                not unavailable and not stale and not self.control_errors
            ),
            "readiness_blockers": {
                "fan_unavailable": unavailable,
                "sensor_stale": stale,
                "control_errors": dict(sorted(self.control_errors.items())),
                "cutover_requirements": [
                    "disable only the legacy humidity automations at cutover",
                    "keep presence, button, evening-air-out, and drying-room automations enabled",
                ],
            },
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "zones": zones,
            "recent_events": self.store.recent_events(),
            "daily_summaries": self.store.daily_summaries(),
        }

    def _round_or_unknown(self, value: float | None, digits: int = 2) -> str | float:
        return "unknown" if value is None else round(value, digits)
