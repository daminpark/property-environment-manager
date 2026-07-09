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
            fingerprint = json.dumps({
                "mode": payload.get("mode"),
                "should_run": payload.get("should_run"),
                "command": payload.get("command"),
                "fan_state_mismatch": payload.get("fan_state_mismatch"),
                "sensor_stale": payload.get("sensor_stale"),
            }, sort_keys=True)
            if self._last_fingerprints.get(zone_id) != fingerprint:
                kind = "mismatch" if payload.get("fan_state_mismatch") else str(payload.get("mode", "state"))
                rows.append((ts, zone_id, kind, fingerprint, json.dumps(payload, sort_keys=True)))
                self._last_fingerprints[zone_id] = fingerprint
            if payload.get("mode") != "idle" or payload.get("fan_state_mismatch") or payload.get("sensor_stale"):
                samples.append((ts, zone_id, json.dumps(payload, sort_keys=True)))
        if not rows and not samples and not summaries:
            return
        with self._connect() as conn:
            if rows:
                conn.executemany("INSERT INTO events(ts, zone_id, kind, fingerprint, payload_json) VALUES (?, ?, ?, ?, ?)", rows)
            if samples:
                conn.executemany("INSERT INTO samples(ts, zone_id, payload_json) VALUES (?, ?, ?)", samples)
            if summaries:
                self._record_daily_summaries(conn, summaries, run_at=run_at_utc)
            raw_cutoff = (run_at_utc - timedelta(days=self.raw_retention_days)).isoformat()
            event_cutoff = (run_at_utc - timedelta(days=self.event_retention_days)).isoformat()
            summary_cutoff = (run_at_utc - timedelta(days=self.summary_retention_days)).date().isoformat()
            conn.execute("DELETE FROM samples WHERE ts < ?", (raw_cutoff,))
            conn.execute("DELETE FROM events WHERE ts < ?", (event_cutoff,))
            conn.execute("DELETE FROM daily_summaries WHERE day < ?", (summary_cutoff,))

    def recent_events(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT ts, zone_id, kind, payload_json FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": ts, "zone_id": zone_id, "kind": kind, "payload": json.loads(payload_json)} for ts, zone_id, kind, payload_json in rows]

    def daily_summaries(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, zone_id, metrics_json, updated_at FROM daily_summaries ORDER BY day DESC, zone_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "day": day,
                "zone_id": zone_id,
                "metrics": json.loads(metrics_json),
                "updated_at": updated_at,
            }
            for day, zone_id, metrics_json, updated_at in rows
        ]

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
        mode = str(payload.get("mode", "unknown"))
        should_run = bool(payload.get("should_run"))
        fan_on = bool(payload.get("fan_on"))
        fan_available = bool(payload.get("fan_available", True))
        fan_mismatch = bool(payload.get("fan_state_mismatch"))
        sensor_stale = bool(payload.get("sensor_stale"))
        delta_absolute_humidity = self._float(payload.get("delta_absolute_humidity"))
        absolute_humidity = self._float(payload.get("absolute_humidity"))
        relative_humidity = self._float(payload.get("relative_humidity"))
        rate = self._float(payload.get("rate_gm3_per_min"))
        real_humidity_event = (
            (delta_absolute_humidity is not None and delta_absolute_humidity >= 1.0)
            or (relative_humidity is not None and relative_humidity >= 75.0)
        )
        rate_only_low_delta = (
            mode == "moisture_rising"
            and should_run
            and (delta_absolute_humidity is None or delta_absolute_humidity < 0.8)
        )
        would_improve_current = should_run and not fan_on and real_humidity_event
        would_be_worse_than_current = fan_on and not should_run and real_humidity_event
        dangerous_miss = not should_run and not fan_on and real_humidity_event
        hard_blocker = sensor_stale or not fan_available
        metrics: dict[str, Any] = {
            "observations": 1,
            "should_run_observations": 1 if should_run else 0,
            "fan_on_observations": 1 if fan_on else 0,
            "fan_unavailable_observations": 0 if fan_available else 1,
            "fan_state_mismatches": 1 if fan_mismatch else 0,
            "should_run_fan_off": 1 if should_run and not fan_on else 0,
            "fan_on_should_not_run": 1 if fan_on and not should_run else 0,
            "sensor_stale_observations": 1 if sensor_stale else 0,
            "moisture_rising_observations": 1 if mode == "moisture_rising" else 0,
            "drying_observations": 1 if mode == "drying" else 0,
            "real_humidity_event_observations": 1 if real_humidity_event else 0,
            "rate_only_low_delta_observations": 1 if rate_only_low_delta else 0,
            "would_improve_current_system": 1 if would_improve_current else 0,
            "would_be_worse_than_current_system": 1 if would_be_worse_than_current else 0,
            "dangerous_miss_candidates": 1 if dangerous_miss else 0,
            "hard_safety_blockers": 1 if hard_blocker else 0,
            "improvement_candidates": 1 if would_improve_current else 0,
            "false_positive_candidates": 1 if should_run and not fan_on and rate_only_low_delta else 0,
        }
        self._set_max(metrics, "max_absolute_humidity", absolute_humidity)
        self._set_max(metrics, "max_relative_humidity", relative_humidity)
        self._set_max(metrics, "max_delta_absolute_humidity", delta_absolute_humidity)
        self._set_max(metrics, "max_rate_gm3_per_min", rate)
        return metrics

    def _merge_metrics(self, existing: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing)
        for key, value in delta.items():
            if key.startswith("max_"):
                if value is not None and (merged.get(key) is None or value > merged[key]):
                    merged[key] = value
            elif key.startswith("min_"):
                if value is not None and (merged.get(key) is None or value < merged[key]):
                    merged[key] = value
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
