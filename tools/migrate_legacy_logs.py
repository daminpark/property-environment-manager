#!/usr/bin/env python3
"""Import legacy controller SQLite logs into the combined add-on database files."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


EVENT_COLUMNS = ("ts", "zone_id", "kind", "fingerprint", "payload_json")
SAMPLE_COLUMNS = ("ts", "zone_id", "payload_json")
SUMMARY_COLUMNS = ("day", "zone_id", "metrics_json", "updated_at")


def ensure_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_day ON daily_summaries(day)"
    )


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def copy_rows(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    dry_run: bool,
) -> int:
    if not table_exists(source, table):
        return 0
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(columns)
    where = " AND ".join(f"{column} = ?" for column in columns)
    rows = source.execute(f"SELECT {col_list} FROM {table}").fetchall()
    copied = 0
    for row in rows:
        exists = destination.execute(
            f"SELECT 1 FROM {table} WHERE {where} LIMIT 1",
            tuple(row),
        ).fetchone()
        if exists:
            continue
        copied += 1
        if dry_run:
            continue
        destination.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
            tuple(row),
        )
    return copied


def copy_daily_summaries(
    source: sqlite3.Connection, destination: sqlite3.Connection, *, dry_run: bool
) -> int:
    if not table_exists(source, "daily_summaries"):
        return 0
    rows = source.execute(
        "SELECT day, zone_id, metrics_json, updated_at FROM daily_summaries"
    ).fetchall()
    copied = 0
    for row in rows:
        copied += 1
        if dry_run:
            continue
        destination.execute(
            """
            INSERT INTO daily_summaries(day, zone_id, metrics_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day, zone_id) DO UPDATE SET
                metrics_json = excluded.metrics_json,
                updated_at = excluded.updated_at
            """,
            tuple(row),
        )
    return copied


def migrate(source_path: Path, destination_path: Path, *, dry_run: bool) -> dict[str, int]:
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination_path) as dest:
        ensure_schema(dest)
        counts = {
            "events": copy_rows(
                source,
                dest,
                table="events",
                columns=EVENT_COLUMNS,
                dry_run=dry_run,
            ),
            "samples": copy_rows(
                source,
                dest,
                table="samples",
                columns=SAMPLE_COLUMNS,
                dry_run=dry_run,
            ),
            "daily_summaries": copy_daily_summaries(source, dest, dry_run=dry_run),
        }
        if dry_run:
            dest.rollback()
        else:
            dest.commit()
        return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    counts = migrate(args.source, args.destination, dry_run=args.dry_run)
    mode = "would import" if args.dry_run else "imported"
    for table, count in counts.items():
        print(f"{mode} {count} {table} rows")


if __name__ == "__main__":
    main()
