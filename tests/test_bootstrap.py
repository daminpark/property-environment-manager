from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from property_environment_manager.bootstrap import LegacyDataSpec, bootstrap_legacy_data
from ventilation_manager.event_store import EventStore


def make_spec(tmp_path: Path) -> LegacyDataSpec:
    source = tmp_path / "share"
    destination = tmp_path / "data"
    source.mkdir()
    destination.mkdir()
    return LegacyDataSpec(
        name="ventilation",
        source_database=source / "legacy.sqlite3",
        destination_database=destination / "events.sqlite3",
        source_state=source / "legacy_state.json",
        destination_state=destination / "state.json",
        marker=destination / ".legacy_imported",
    )


def test_bootstrap_copies_database_wal_and_state_once(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    source_store = EventStore(spec.source_database)
    source_store.record_run(
        [
            {
                "zone_id": "a",
                "mode": "idle",
                "should_run": False,
                "command": "none",
                "fan_available": True,
                "fan_on": False,
                "fan_state_mismatch": False,
                "sensor_stale": False,
            }
        ],
        run_at=datetime.now(UTC),
    )
    spec.source_state.write_text(json.dumps({"version": 1, "zones": {}}))

    first = bootstrap_legacy_data((spec,))
    second = bootstrap_legacy_data((spec,))

    assert first == {"ventilation": "imported"}
    assert second == {"ventilation": "already_imported"}
    with sqlite3.connect(spec.destination_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert json.loads(spec.destination_state.read_text()) == {
        "version": 1,
        "zones": {},
    }


def test_bootstrap_refuses_to_overwrite_existing_database(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    EventStore(spec.source_database)
    spec.destination_database.write_bytes(b"existing")

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        bootstrap_legacy_data((spec,))


def test_bootstrap_recovers_database_installed_before_marker(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    EventStore(spec.destination_database)
    spec.source_state.write_text(json.dumps({"version": 1, "zones": {}}))
    pending_marker = spec.marker.with_name(f"{spec.marker.name}.importing")
    pending_marker.write_text(json.dumps({"source": "legacy.sqlite3"}))

    result = bootstrap_legacy_data((spec,))

    assert result == {"ventilation": "recovered_import"}
    assert spec.marker.exists()
    assert not pending_marker.exists()
    assert json.loads(spec.destination_state.read_text()) == {
        "version": 1,
        "zones": {},
    }


def test_bootstrap_keeps_valid_database_when_optional_state_is_invalid(
    tmp_path: Path,
) -> None:
    spec = make_spec(tmp_path)
    EventStore(spec.source_database)
    spec.source_state.write_text("not json")

    result = bootstrap_legacy_data((spec,))

    assert result == {"ventilation": "imported"}
    assert spec.destination_database.exists()
    assert spec.marker.exists()
    assert not spec.destination_state.exists()
