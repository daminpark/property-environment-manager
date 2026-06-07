# Property Environment Manager

Property Environment Manager is a Home Assistant add-on for property-scale
environmental automation. It combines two observer-first controllers:

- ventilation control for bathroom, toilet, and kitchen humidity events;
- TRV heating policy observation for guest rooms, service rooms, and a drying room.

The add-on is designed for staged deployment. It publishes diagnostics and writes
structured SQLite logs before any device writes are enabled. Active control is
split by subsystem, and TRV calendar-policy writes have a separate explicit gate.

## Design

The repository keeps the proven controller modules independent and runs them
under one add-on process:

```text
property_environment_manager/
  config.yaml                 Home Assistant add-on options
  run.sh                      add-on environment mapping
  src/
    property_environment_manager/
      main.py                 combined runner
      web.py                  combined read-only dashboard/API
    ventilation_manager/      humidity/fan controller
    trv_regulator/            heating/TRV controller
tools/
  migrate_legacy_logs.py      imports old observer SQLite logs
  sanitize_sqlite.py          creates redacted SQLite copies
tests/
```

The combined runner deliberately avoids merging the controller internals. That
keeps the first unified add-on easy to review: each subsystem keeps its own
configuration, runtime state, database, tests, and active-control switch.

## Safety Model

Default installation is observer-only:

- `vent_active_control: false`
- `trv_active_control: false`
- `trv_active_calendar_policy: false`

Ventilation writes require `vent_active_control: true`.

TRV drying-room writes require `trv_active_control: true`.

TRV calendar, guest-limit, service-default, force-heat, and child-lock writes
require both:

- `trv_active_control: true`
- `trv_active_calendar_policy: true`

This means calendar-policy parity can be observed and reviewed without granting
the add-on broad heating authority.

## Diagnostics

The add-on exposes one ingress dashboard and API:

- `/api/status` for combined status
- `/api/ventilation/status` for ventilation-only status
- `/api/trv/status` for TRV-only status
- `/health` for a simple health check

Each controller records local SQLite diagnostics under `/data`:

- `/data/ventilation_manager_events.sqlite3`
- `/data/trv_regulator_events.sqlite3`

Raw samples are retained for short-term analysis. Event and daily-summary rows
are retained longer so rollout decisions can be based on observed behavior
rather than one-off incidents.

## Legacy Log Migration

Do not commit raw production databases. To preserve history from the old
standalone add-ons, copy the old SQLite files to a local working directory or to
`/share`, then import them into the new database files:

```bash
python tools/migrate_legacy_logs.py \
  --source old_ventilation_manager_events.sqlite3 \
  --destination new_ventilation_manager_events.sqlite3

python tools/migrate_legacy_logs.py \
  --source old_trv_regulator_events.sqlite3 \
  --destination new_trv_regulator_events.sqlite3
```

The importer deduplicates event and sample rows by content. Daily summaries are
upserted by day and zone.

## Sanitized Data

For public examples or external review, create redacted copies:

```bash
python tools/sanitize_sqlite.py \
  --source ventilation_manager_events.sqlite3 \
  --destination sanitized_ventilation.sqlite3
```

The sanitizer removes or generalizes common private fields such as booking
summaries, guest names, bearer tokens, IP addresses, and house-specific entity
prefixes. Sanitized output should still be reviewed before publishing.

## Development

Run tests from the repository root:

```bash
PYTHONPATH=property_environment_manager/src python -m pytest -q
```

Build validation:

```bash
PYTHONPATH=property_environment_manager/src python -m compileall -q property_environment_manager/src tests tools
```
