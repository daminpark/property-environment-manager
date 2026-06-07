from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ventilation_manager.event_store import EventStore


def test_daily_summary_separates_improvements_from_low_delta_risk(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    store.record_run(
        [
            {
                "zone_id": "a",
                "mode": "moisture_rising",
                "should_run": True,
                "command": "turn_on",
                "reason": "fast rise from small delta",
                "baseline_absolute_humidity": 10.0,
                "event_baseline_absolute_humidity": 10.0,
                "absolute_humidity": 10.3,
                "relative_humidity": 58.0,
                "delta_absolute_humidity": 0.3,
                "rate_gm3_per_min": 0.09,
                "sensor_stale": False,
                "sample_age_minutes": 1.0,
                "fan_on": False,
                "fan_state_mismatch": True,
            },
            {
                "zone_id": "b",
                "mode": "drying",
                "should_run": True,
                "command": "keep_on",
                "reason": "real humidity still high",
                "baseline_absolute_humidity": 10.0,
                "event_baseline_absolute_humidity": 10.0,
                "absolute_humidity": 12.0,
                "relative_humidity": 78.0,
                "delta_absolute_humidity": 2.0,
                "rate_gm3_per_min": 0.01,
                "sensor_stale": False,
                "sample_age_minutes": 1.0,
                "fan_on": False,
                "fan_state_mismatch": True,
            },
        ],
        run_at=run_at,
    )

    summaries = {item["zone_id"]: item["metrics"] for item in store.daily_summaries()}
    assert summaries["a"]["false_positive_candidates"] == 1
    assert summaries["a"]["would_improve_current_system"] == 0
    assert summaries["b"]["would_improve_current_system"] == 1
    assert summaries["b"]["would_be_worse_than_current_system"] == 0
    assert summaries["b"]["false_positive_candidates"] == 0
    assert summaries["b"]["max_delta_absolute_humidity"] == 2.0


def test_daily_summary_records_idle_observations_without_events(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    idle = {
        "zone_id": "k",
        "mode": "idle",
        "should_run": False,
        "command": "none",
        "reason": "stable",
        "absolute_humidity": 10.0,
        "relative_humidity": 50.0,
        "delta_absolute_humidity": 0.0,
        "rate_gm3_per_min": 0.0,
        "sensor_stale": False,
        "fan_on": False,
        "fan_state_mismatch": False,
    }

    store.record_run([idle], run_at=run_at)
    store.record_run([idle], run_at=run_at)

    summary = store.daily_summaries()[0]["metrics"]
    assert summary["observations"] == 2
    assert summary["should_run_observations"] == 0
    assert store.recent_events(limit=10)



def test_daily_summary_marks_worse_when_observer_would_stop_real_event(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    store.record_run(
        [
            {
                "zone_id": "b",
                "mode": "idle",
                "should_run": False,
                "command": "turn_off",
                "reason": "back near baseline",
                "absolute_humidity": 12.4,
                "relative_humidity": 78.0,
                "delta_absolute_humidity": 1.4,
                "rate_gm3_per_min": 0.0,
                "sensor_stale": False,
                "fan_on": True,
                "fan_state_mismatch": True,
            }
        ],
        run_at=run_at,
    )

    metrics = store.daily_summaries()[0]["metrics"]
    assert metrics["would_be_worse_than_current_system"] == 1
    assert metrics["dangerous_miss_candidates"] == 0
