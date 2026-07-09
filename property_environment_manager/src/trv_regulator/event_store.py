"""Compact local event storage for observer diagnostics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


class EventStore:
    """Small SQLite store for non-HA-recorder observer history."""

    def __init__(
        self,
        path: Path,
        *,
        raw_retention_days: int = 45,
        event_retention_days: int = 400,
        summary_retention_days: int = 400,
    ) -> None:
        self.path = path
        self.raw_retention_days = raw_retention_days
        self.event_retention_days = event_retention_days
        self.summary_retention_days = summary_retention_days
        self._last_fingerprints: dict[str, str] = {}
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    day TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(day, zone_id)
                )
                """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_day ON daily_summaries(day)")

    def record_run(self, decisions: Iterable[Any], *, run_at: datetime) -> None:
        rows: list[tuple[str, str, str, str, str]] = []
        samples: list[tuple[str, str, str]] = []
        summaries: dict[str, dict[str, Any]] = {}
        run_at_utc = run_at.astimezone(UTC)
        ts = run_at_utc.isoformat()
        for decision in decisions:
            payload = self._payload(decision)
            zone_id = str(payload.get("zone_id", "unknown"))
            summaries[zone_id] = self._merge_metrics(
                summaries.get(zone_id, {}), self._summary_delta(zone_id, payload)
            )
            fingerprint = json.dumps(
                {
                    "mode": payload.get("mode"),
                    "suggested_action": payload.get("suggested_action"),
                    "suggested_target_temperature_c": payload.get("suggested_target_temperature_c"),
                    "climate_available": payload.get("climate_available"),
                    "sensor_stale": payload.get("sensor_stale"),
                    "window_open_risk": payload.get("window_open_risk"),
                    "heating_ineffective": payload.get("heating_ineffective"),
                    "calendar_policy_state": payload.get("calendar_policy_state"),
                    "calendar_policy_action": payload.get("calendar_policy_action"),
                },
                sort_keys=True,
            )
            mode = str(payload.get("mode", "state"))
            if self._last_fingerprints.get(zone_id) != fingerprint:
                rows.append((ts, zone_id, mode, fingerprint, json.dumps(payload, sort_keys=True)))
                self._last_fingerprints[zone_id] = fingerprint
            if mode != "idle" or payload.get("sensor_stale") or payload.get("window_open_risk") or payload.get("heating_ineffective"):
                samples.append((ts, zone_id, json.dumps(payload, sort_keys=True)))
        if not rows and not samples and not summaries:
            return
        with self._connect() as conn:
            if rows:
                conn.executemany(
                    "INSERT INTO events(ts, zone_id, kind, fingerprint, payload_json) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            if samples:
                conn.executemany(
                    "INSERT INTO samples(ts, zone_id, payload_json) VALUES (?, ?, ?)",
                    samples,
                )
            if summaries:
                self._record_daily_summaries(conn, summaries, run_at=run_at_utc)
            raw_cutoff = (run_at_utc - timedelta(days=self.raw_retention_days)).isoformat()
            event_cutoff = (run_at_utc - timedelta(days=self.event_retention_days)).isoformat()
            summary_cutoff = (run_at_utc - timedelta(days=self.summary_retention_days)).date().isoformat()
            conn.execute("DELETE FROM samples WHERE ts < ?", (raw_cutoff,))
            conn.execute("DELETE FROM events WHERE ts < ?", (event_cutoff,))
            conn.execute("DELETE FROM daily_summaries WHERE day < ?", (summary_cutoff,))

    def recent_events(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, zone_id, kind, payload_json FROM events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"ts": ts, "zone_id": zone_id, "kind": kind, "payload": json.loads(payload_json)}
            for ts, zone_id, kind, payload_json in rows
        ]

    def daily_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, zone_id, metrics_json, updated_at FROM daily_summaries ORDER BY day DESC, zone_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        summaries = []
        for day, zone_id, metrics_json, updated_at in rows:
            metrics = json.loads(metrics_json)
            heating_response_count = metrics.get("heating_response_observations", 0)
            if heating_response_count:
                metrics["heating_response_avg_c"] = round(
                    metrics.get("heating_response_sum_c", 0.0) / heating_response_count,
                    3,
                )
            summaries.append(
                {
                    "day": day,
                    "zone_id": zone_id,
                    "metrics": metrics,
                    "updated_at": updated_at,
                }
            )
        return summaries

    def _record_daily_summaries(
        self,
        conn: sqlite3.Connection,
        summaries: dict[str, dict[str, Any]],
        *,
        run_at: datetime,
    ) -> None:
        day = run_at.date().isoformat()
        updated_at = run_at.isoformat()
        for zone_id, delta in summaries.items():
            row = conn.execute(
                "SELECT metrics_json FROM daily_summaries WHERE day = ? AND zone_id = ?",
                (day, zone_id),
            ).fetchone()
            metrics = json.loads(row[0]) if row else {}
            metrics = self._merge_metrics(metrics, delta)
            conn.execute(
                """
                INSERT INTO daily_summaries(day, zone_id, metrics_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, zone_id) DO UPDATE SET
                    metrics_json = excluded.metrics_json,
                    updated_at = excluded.updated_at
                """,
                (day, zone_id, json.dumps(metrics, sort_keys=True), updated_at),
            )

    def _summary_delta(self, zone_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if zone_id == "boiler":
            return self._boiler_summary_delta(payload)
        mode = str(payload.get("mode", "unknown"))
        suggested_action = str(payload.get("suggested_action", "none"))
        sensor_stale = bool(payload.get("sensor_stale"))
        climate_available = bool(payload.get("climate_available"))
        boiler_on = bool(payload.get("boiler_on"))
        boiler_available = bool(payload.get("boiler_available", True))
        window_open_risk = bool(payload.get("window_open_risk"))
        heating_ineffective = bool(payload.get("heating_ineffective"))
        target_temperature = self._float(payload.get("target_temperature_c"))
        suggested_target = self._float(payload.get("suggested_target_temperature_c"))
        calendar_policy_state = payload.get("calendar_policy_state")
        calendar_policy_action = str(payload.get("calendar_policy_action") or "none")
        heating_response = self._float(payload.get("heating_response_c"))
        room_temperature = self._float(payload.get("room_temperature_c"))
        absolute_humidity = self._float(payload.get("absolute_humidity_gm3"))
        relative_humidity = self._float(payload.get("relative_humidity_percent"))
        drying_mode = mode in {"drying_watch", "drying_elevated", "drying_severe", "drying_recovered"}
        drying_boost = suggested_action == "would_raise_drying_target"
        hard_blocker = sensor_stale or not climate_available or not boiler_available
        write_blocker = hard_blocker or window_open_risk or heating_ineffective
        safe_drying_boost = (
            zone_id == "z"
            and drying_boost
            and climate_available
            and not sensor_stale
            and not window_open_risk
            and not heating_ineffective
        )
        unsafe_drying_boost = drying_boost and not safe_drying_boost
        current_ha_policy_action = suggested_action in {
            "would_set_calendar_checkin_target",
            "would_set_calendar_checkout_target",
            "would_enforce_guest_max_target",
            "would_enforce_guest_min_target",
            "would_restore_service_default",
            "would_set_hvac_mode_heat",
            "would_restore_child_lock",
        }
        calendar_policy_observed = calendar_policy_state not in {None, "unknown"}
        calendar_policy_recommendation = (
            calendar_policy_action != "none" or suggested_action in {
                "would_set_calendar_checkin_target",
                "would_set_calendar_checkout_target",
            }
        )
        metrics: dict[str, Any] = {
            "observations": 1,
            "boiler_on_observations": 1 if boiler_on else 0,
            "boiler_unavailable_observations": 0 if boiler_available else 1,
            "climate_unavailable_observations": 0 if climate_available else 1,
            "sensor_stale_observations": 1 if sensor_stale else 0,
            "window_open_risk_observations": 1 if window_open_risk else 0,
            "heating_ineffective_observations": 1 if heating_ineffective else 0,
            "heating_observed_observations": 1 if mode == "heating_observed" else 0,
            "drying_watch_observations": 1 if mode == "drying_watch" else 0,
            "drying_elevated_observations": 1 if mode == "drying_elevated" else 0,
            "drying_severe_observations": 1 if mode == "drying_severe" else 0,
            "drying_recovered_observations": 1 if mode == "drying_recovered" else 0,
            "drying_observations": 1 if drying_mode else 0,
            "drying_boost_recommendations": 1 if drying_boost else 0,
            "safe_drying_boost_candidates": 1 if safe_drying_boost else 0,
            "unsafe_drying_boost_candidates": 1 if unsafe_drying_boost else 0,
            "calendar_policy_observations": 1 if calendar_policy_observed else 0,
            "calendar_policy_recommendations": 1 if calendar_policy_recommendation else 0,
            "calendar_active_booking_observations": (
                1 if payload.get("calendar_policy_active_booking") else 0
            ),
            "calendar_renovation_suppressed_observations": (
                1 if payload.get("calendar_policy_suppressed_by_renovation") else 0
            ),
            "guest_limit_recommendations": (
                1 if suggested_action.startswith("would_enforce_guest_") else 0
            ),
            "service_default_recommendations": (
                1 if suggested_action == "would_restore_service_default" else 0
            ),
            "force_heat_recommendations": (
                1 if suggested_action == "would_set_hvac_mode_heat" else 0
            ),
            "child_lock_recommendations": (
                1 if suggested_action == "would_restore_child_lock" else 0
            ),
            "would_match_current_ha_policy": 1 if current_ha_policy_action else 0,
            "would_improve_current_system": 1 if safe_drying_boost else 0,
            "would_be_worse_than_current_system": 1 if unsafe_drying_boost else 0,
            "hard_safety_blockers": 1 if hard_blocker else 0,
            "unsafe_write_blockers": 1 if write_blocker else 0,
        }
        if heating_response is not None:
            metrics["heating_response_observations"] = 1
            metrics["heating_response_sum_c"] = heating_response
            metrics["min_heating_response_c"] = heating_response
            metrics["max_heating_response_c"] = heating_response
        if target_temperature is not None:
            metrics["target_temperature_values_c"] = [round(target_temperature, 1)]
        if suggested_target is not None:
            metrics["suggested_temperature_values_c"] = [round(suggested_target, 1)]
        self._set_max(metrics, "max_room_temperature_c", room_temperature)
        self._set_max(metrics, "max_absolute_humidity_gm3", absolute_humidity)
        self._set_max(metrics, "max_relative_humidity_percent", relative_humidity)
        return metrics

    def _boiler_summary_delta(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("suggested_action", "none"))
        available = bool(payload.get("boiler_available"))
        safe = bool(payload.get("control_safe"))
        unavailable_zones = payload.get("unavailable_zone_ids") or []
        return {
            "observations": 1,
            "boiler_unavailable_observations": 0 if available else 1,
            "boiler_on_observations": 1 if payload.get("boiler_on") else 0,
            "boiler_should_be_on_observations": (
                1 if payload.get("boiler_should_be_on") else 0
            ),
            "boiler_state_mismatches": (
                1 if action in {"would_turn_boiler_on", "would_turn_boiler_off"} else 0
            ),
            "boiler_turn_on_recommendations": 1 if action == "would_turn_boiler_on" else 0,
            "boiler_turn_off_recommendations": 1 if action == "would_turn_boiler_off" else 0,
            "boiler_blocked_commands": 1 if action.startswith("blocked_") else 0,
            "trv_unavailable_observations": 1 if unavailable_zones else 0,
            "hard_safety_blockers": 0 if available and safe else 1,
        }

    def _merge_metrics(self, existing: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing)
        for key, value in delta.items():
            if key.startswith("max_"):
                if value is not None and (merged.get(key) is None or value > merged[key]):
                    merged[key] = value
            elif key.startswith("min_"):
                if value is not None and (merged.get(key) is None or value < merged[key]):
                    merged[key] = value
            elif isinstance(value, list):
                values = list(merged.get(key, [])) + value
                merged[key] = sorted({item for item in values})[:24]
            elif isinstance(value, (int, float)):
                prior = merged.get(key, 0)
                merged[key] = prior + value
            else:
                merged[key] = value
        return merged

    def _set_max(self, metrics: dict[str, Any], key: str, value: float | None) -> None:
        if value is not None:
            metrics[key] = value

    def _float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _payload(self, decision: Any) -> dict[str, Any]:
        data = asdict(decision) if is_dataclass(decision) else dict(decision)
        return {key: self._jsonable(value) for key, value in data.items()}

    def _jsonable(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        return value
