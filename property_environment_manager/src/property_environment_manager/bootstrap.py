"""First-start import of legacy standalone controller data."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacyDataSpec:
    """Source and destination files for one controller."""

    name: str
    source_database: Path
    destination_database: Path
    source_state: Path
    destination_state: Path
    marker: Path


def default_legacy_specs() -> tuple[LegacyDataSpec, ...]:
    share = Path(
        os.getenv(
            "PEM_LEGACY_DATA_DIR",
            "/share/property_environment_manager",
        )
    )
    data = Path(os.getenv("PEM_DATA_DIR", "/data"))
    return (
        LegacyDataSpec(
            name="ventilation",
            source_database=share / "legacy_ventilation_manager_events.sqlite3",
            destination_database=data / "ventilation_manager_events.sqlite3",
            source_state=share / "legacy_ventilation_manager_state.json",
            destination_state=data / "ventilation_manager_state.json",
            marker=data / ".legacy_ventilation_imported",
        ),
        LegacyDataSpec(
            name="trv",
            source_database=share / "legacy_trv_regulator_events.sqlite3",
            destination_database=data / "trv_regulator_events.sqlite3",
            source_state=share / "legacy_trv_regulator_state.json",
            destination_state=data / "trv_regulator_state.json",
            marker=data / ".legacy_trv_imported",
        ),
    )


def bootstrap_legacy_data(
    specs: tuple[LegacyDataSpec, ...] | None = None,
) -> dict[str, str]:
    """Import each legacy database exactly once before controllers start."""

    results: dict[str, str] = {}
    for spec in specs or default_legacy_specs():
        results[spec.name] = _bootstrap_spec(spec)
    return results


def _bootstrap_spec(spec: LegacyDataSpec) -> str:
    if spec.marker.exists():
        return "already_imported"
    pending_marker = spec.marker.with_name(f"{spec.marker.name}.importing")
    if spec.destination_database.exists() and pending_marker.exists():
        _validate_database(spec.destination_database, spec.name)
        os.replace(pending_marker, spec.marker)
        _copy_optional_state(spec)
        return "recovered_import"
    if not spec.source_database.exists():
        return "source_not_found"
    if spec.destination_database.exists():
        raise RuntimeError(
            f"Refusing to overwrite existing {spec.name} database: "
            f"{spec.destination_database}"
        )

    spec.destination_database.parent.mkdir(parents=True, exist_ok=True)
    importing = spec.destination_database.with_suffix(".sqlite3.importing")
    importing.unlink(missing_ok=True)
    pending_marker.unlink(missing_ok=True)
    destination_installed = False
    try:
        with sqlite3.connect(spec.source_database) as source, sqlite3.connect(
            importing
        ) as destination:
            source.backup(destination)
        _validate_database(importing, spec.name)
        pending_marker.write_text(
            json.dumps(
                {
                    "source": str(spec.source_database),
                    "source_size": spec.source_database.stat().st_size,
                },
                sort_keys=True,
            )
        )
        os.replace(importing, spec.destination_database)
        destination_installed = True
        os.replace(pending_marker, spec.marker)
        _copy_optional_state(spec)
    except Exception:
        importing.unlink(missing_ok=True)
        if not destination_installed:
            pending_marker.unlink(missing_ok=True)
        raise

    LOGGER.info(
        "Imported legacy %s database from %s",
        spec.name,
        spec.source_database,
    )
    return "imported"


def _validate_database(database: Path, name: str) -> None:
    with sqlite3.connect(database) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise RuntimeError(f"Imported {name} database failed integrity_check: {result}")


def _copy_optional_state(spec: LegacyDataSpec) -> None:
    try:
        _copy_state_if_available(spec)
    except Exception:
        # Runtime state is useful for continuity, but the validated event
        # history is the migration's durable asset. A bad optional state
        # file must not strand an otherwise successful database import.
        LOGGER.exception("Could not import legacy %s state", spec.name)


def _copy_state_if_available(spec: LegacyDataSpec) -> None:
    if not spec.source_state.exists() or spec.destination_state.exists():
        return
    json.loads(spec.source_state.read_text())
    importing = spec.destination_state.with_suffix(".json.importing")
    shutil.copy2(spec.source_state, importing)
    os.replace(importing, spec.destination_state)
