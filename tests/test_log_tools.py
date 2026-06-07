from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools.migrate_legacy_logs import ensure_schema, migrate
from tools.sanitize_sqlite import sanitize_database, sanitize_value


def test_sanitize_value_redacts_private_calendar_summary() -> None:
    payload = {
        "calendar_policy_entity_id": "calendar.195_1_calendar",
        "calendar_policy_event_summary": "Jane Example",
        "reason": "calendar.195_1_calendar checkin trigger for Jane Example",
        "fan_entity": "switch.193_a_fan",
        "url": "http://192.0.2.10/api",
    }

    sanitized = sanitize_value(payload)

    assert sanitized["calendar_policy_entity_id"] == "calendar.house_b_1_calendar"
    assert sanitized["calendar_policy_event_summary"] == "[redacted]"
    assert sanitized["reason"].endswith("for [redacted booking]")
    assert sanitized["fan_entity"] == "switch.house_a_a_fan"
    assert sanitized["url"] == "http://0.0.0.0/api"


def test_migrate_legacy_logs_deduplicates_events(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    with sqlite3.connect(source) as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO events(ts, zone_id, kind, fingerprint, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("2026-06-07T10:00:00+00:00", "a", "drying", "fp", "{}"),
        )
        conn.commit()

    first = migrate(source, destination, dry_run=False)
    second = migrate(source, destination, dry_run=False)

    assert first["events"] == 1
    assert second["events"] == 0


def test_sanitize_database_redacts_payload_json(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "sanitized.sqlite3"
    with sqlite3.connect(source) as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO events(ts, zone_id, kind, fingerprint, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "2026-06-07T10:00:00+00:00",
                "a",
                "calendar",
                "fp",
                json.dumps({"summary": "Jane Example", "entity": "sensor.195_a"}),
            ),
        )
        conn.commit()

    sanitize_database(source, destination, overwrite=False)

    with sqlite3.connect(destination) as conn:
        payload_json = conn.execute("SELECT payload_json FROM events").fetchone()[0]

    payload = json.loads(payload_json)
    assert payload["summary"] == "[redacted]"
    assert payload["entity"] == "sensor.house_b_a"
