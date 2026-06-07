#!/usr/bin/env python3
"""Create a sanitized copy of controller observer logs."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from tools.migrate_legacy_logs import ensure_schema, table_exists


SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "calendar_policy_event_summary",
    "event_summary",
    "guest",
    "guest_name",
    "message",
    "name",
    "password",
    "secret",
    "summary",
    "title",
    "token",
}

HOUSE_REPLACEMENTS = (
    ("193195", "house_a_house_b"),
    ("193", "house_a"),
    ("195", "house_b"),
)

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def sanitize_text(value: str) -> str:
    sanitized = value
    sanitized = BEARER_RE.sub("Bearer [redacted]", sanitized)
    sanitized = EMAIL_RE.sub("[redacted-email]", sanitized)
    sanitized = IP_RE.sub("0.0.0.0", sanitized)
    for original, replacement in HOUSE_REPLACEMENTS:
        sanitized = re.sub(rf"(?<!\d){re.escape(original)}(?!\d)", replacement, sanitized)
    if "calendar." in sanitized and " for " in sanitized:
        sanitized = re.sub(r" for .+$", " for [redacted booking]", sanitized)
    return sanitized


def sanitize_value(value: Any, *, key: str | None = None) -> Any:
    normalized_key = key.lower() if key else ""
    if normalized_key in SENSITIVE_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(child_key): sanitize_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_payload_json(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return sanitize_text(payload_json)
    return json.dumps(sanitize_value(payload), sort_keys=True)


def sanitize_database(source_path: Path, destination_path: Path, *, overwrite: bool) -> None:
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if destination_path.exists() and not overwrite:
        raise FileExistsError(destination_path)
    if destination_path.exists():
        destination_path.unlink()
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(source_path) as source, sqlite3.connect(destination_path) as dest:
        ensure_schema(dest)
        if table_exists(source, "events"):
            for ts, zone_id, kind, fingerprint, payload_json in source.execute(
                "SELECT ts, zone_id, kind, fingerprint, payload_json FROM events"
            ):
                dest.execute(
                    """
                    INSERT INTO events(ts, zone_id, kind, fingerprint, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        sanitize_text(ts),
                        sanitize_text(zone_id),
                        sanitize_text(kind),
                        sanitize_text(fingerprint),
                        sanitize_payload_json(payload_json),
                    ),
                )
        if table_exists(source, "samples"):
            for ts, zone_id, payload_json in source.execute(
                "SELECT ts, zone_id, payload_json FROM samples"
            ):
                dest.execute(
                    "INSERT INTO samples(ts, zone_id, payload_json) VALUES (?, ?, ?)",
                    (
                        sanitize_text(ts),
                        sanitize_text(zone_id),
                        sanitize_payload_json(payload_json),
                    ),
                )
        if table_exists(source, "daily_summaries"):
            for day, zone_id, metrics_json, updated_at in source.execute(
                "SELECT day, zone_id, metrics_json, updated_at FROM daily_summaries"
            ):
                dest.execute(
                    """
                    INSERT INTO daily_summaries(day, zone_id, metrics_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        sanitize_text(day),
                        sanitize_text(zone_id),
                        sanitize_payload_json(metrics_json),
                        sanitize_text(updated_at),
                    ),
                )
        dest.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    sanitize_database(args.source, args.destination, overwrite=args.overwrite)
    print(f"wrote sanitized database to {args.destination}")


if __name__ == "__main__":
    main()
