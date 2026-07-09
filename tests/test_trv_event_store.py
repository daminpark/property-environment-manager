from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trv_regulator.event_store import EventStore


def test_daily_summary_tracks_safe_drying_boost_candidates(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    store.record_run(
        [
            {
                "zone_id": "z",
                "mode": "drying_severe",
                "suggested_action": "would_raise_drying_target",
                "suggested_target_temperature_c": 26.0,
                "reason": "drying room absolute humidity severe",
                "room_temperature_c": 22.0,
                "trv_current_temperature_c": 22.0,
                "target_temperature_c": 22.0,
                "hvac_action": "idle",
                "boiler_on": True,
                "room_temperature_rate_c_per_hour": 0.1,
                "heating_response_c": 0.2,
                "window_open_risk": False,
                "heating_ineffective": False,
                "absolute_humidity_gm3": 17.0,
                "relative_humidity_percent": 76.0,
                "absolute_humidity_rate_gm3_per_min": 0.01,
                "sensor_stale": False,
                "active_control": False,
                "climate_available": True,
                "room_sample_age_minutes": 1.0,
                "humidity_sample_age_minutes": 1.0,
            }
        ],
        run_at=run_at,
    )

    metrics = store.daily_summaries()[0]["metrics"]
    assert metrics["drying_severe_observations"] == 1
    assert metrics["drying_boost_recommendations"] == 1
    assert metrics["safe_drying_boost_candidates"] == 1
    assert metrics["would_improve_current_system"] == 1
    assert metrics["would_be_worse_than_current_system"] == 0
    assert metrics["heating_response_avg_c"] == 0.2
    assert metrics["target_temperature_values_c"] == [22.0]


def test_daily_summary_marks_blockers_for_unsafe_writes(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    store.record_run(
        [
            {
                "zone_id": "k",
                "mode": "sensor_unavailable",
                "suggested_action": "none",
                "suggested_target_temperature_c": None,
                "reason": "TRV climate entity unavailable",
                "room_temperature_c": None,
                "trv_current_temperature_c": None,
                "target_temperature_c": None,
                "hvac_action": None,
                "boiler_on": False,
                "room_temperature_rate_c_per_hour": None,
                "heating_response_c": None,
                "window_open_risk": False,
                "heating_ineffective": False,
                "absolute_humidity_gm3": None,
                "relative_humidity_percent": None,
                "absolute_humidity_rate_gm3_per_min": None,
                "sensor_stale": True,
                "active_control": False,
                "climate_available": False,
                "room_sample_age_minutes": None,
                "humidity_sample_age_minutes": None,
            }
        ],
        run_at=run_at,
    )

    metrics = store.daily_summaries()[0]["metrics"]
    assert metrics["sensor_stale_observations"] == 1
    assert metrics["climate_unavailable_observations"] == 1
    assert metrics["hard_safety_blockers"] == 1
    assert metrics["unsafe_write_blockers"] == 1



def test_daily_summary_marks_worse_when_drying_boost_has_blockers(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)

    store.record_run(
        [
            {
                "zone_id": "z",
                "mode": "drying_severe",
                "suggested_action": "would_raise_drying_target",
                "suggested_target_temperature_c": 26.0,
                "reason": "drying room absolute humidity severe but stale",
                "room_temperature_c": 22.0,
                "trv_current_temperature_c": 22.0,
                "target_temperature_c": 22.0,
                "hvac_action": "idle",
                "boiler_on": True,
                "room_temperature_rate_c_per_hour": None,
                "heating_response_c": None,
                "window_open_risk": False,
                "heating_ineffective": False,
                "absolute_humidity_gm3": 17.0,
                "relative_humidity_percent": 76.0,
                "absolute_humidity_rate_gm3_per_min": None,
                "sensor_stale": True,
                "active_control": False,
                "climate_available": True,
                "room_sample_age_minutes": 60.0,
                "humidity_sample_age_minutes": 60.0,
            }
        ],
        run_at=run_at,
    )

    metrics = store.daily_summaries()[0]["metrics"]
    assert metrics["unsafe_drying_boost_candidates"] == 1
    assert metrics["would_be_worse_than_current_system"] == 1
    assert metrics["would_improve_current_system"] == 0


def test_daily_summary_records_blocked_unavailable_boiler(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    run_at = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)

    store.record_run(
        [
            {
                "zone_id": "boiler",
                "mode": "boiler_unavailable",
                "suggested_action": "blocked_boiler_unavailable",
                "reason": "boiler relay unavailable; no command is safe",
                "boiler_on": False,
                "boiler_available": False,
                "boiler_should_be_on": True,
                "demanding_zone_ids": ["z"],
                "unavailable_zone_ids": [],
                "control_safe": False,
                "state_mismatch": False,
                "active_control": False,
            }
        ],
        run_at=run_at,
    )

    summary = store.daily_summaries()[0]
    assert summary["zone_id"] == "boiler"
    assert summary["metrics"]["boiler_unavailable_observations"] == 1
    assert summary["metrics"]["boiler_blocked_commands"] == 1
    assert summary["metrics"]["hard_safety_blockers"] == 1
