# Property Environment Manager

[![Checks](https://github.com/daminpark/property-environment-manager/actions/workflows/checks.yml/badge.svg)](https://github.com/daminpark/property-environment-manager/actions/workflows/checks.yml)

Observer-first Home Assistant add-on for humidity ventilation and TRV heating
policy.

This is a practical property-operations automation project: it watches humidity,
heating, calendar, and room-state signals, explains what it would do, and only
touches real devices after explicit active-control gates are enabled. I built it
to replace fragile one-off automations with something observable, staged, and
reviewable before trusting it with physical controls.

## What It Does

- Detects bathroom, toilet, and kitchen humidity events using absolute humidity,
  learned baselines, rise rates, stale-sensor checks, and minimum run-time rules.
- Observes TRV heating behaviour for guest rooms, service rooms, and a drying
  room, including suspected open windows, ineffective heating, child-lock drift,
  force-heat recovery, and drying-room humidity boosts.
- Mirrors calendar-based heating policy in observer mode before calendar writes
  are allowed.
- Publishes Home Assistant diagnostic entities, a read-only ingress dashboard,
  and JSON status APIs for review.
- Writes local SQLite event, sample, and daily-summary logs so rollout decisions
  can be based on observed behaviour rather than a single incident.
- Includes migration and sanitisation tools for carrying forward old observer
  logs without publishing raw private data.

## Current Status

This is a personal, working add-on rather than a packaged commercial product.
The combined add-on is at `0.1.0`; the two controller modules came from earlier
standalone add-ons and are intentionally still easy to review independently.

Default installation is observer-only:

- `vent_active_control: false`
- `trv_active_control: false`
- `trv_active_boiler_control: false`
- `trv_active_calendar_policy: false`

Ventilation writes require `vent_active_control: true`. TRV drying-room writes
require `trv_active_control: true`. Calendar, guest-limit, service-default,
force-heat, and child-lock writes require both `trv_active_control: true` and
`trv_active_calendar_policy: true`. Boiler writes require both
`trv_active_control: true` and the independent
`trv_active_boiler_control: true` gate.

The current ventilation control scope is humidity only. Presence, button,
evening-air-out, and drying-room routines remain Home Assistant-owned during the
first staged cutover. The dashboard reports these ownership requirements rather
than claiming full replacement readiness.

## Demo

The demo artifacts are synthetic and redacted. They are meant to show the shape
of the diagnostics without exposing a real property, booking, token, or Home
Assistant entity namespace.

![Synthetic dashboard screenshot](demo/dashboard-synthetic.jpg)

- [Synthetic status payload](demo/status.json)
- [Architecture note](docs/architecture.md)
- [Privacy and safety note](docs/privacy-and-safety.md)

## Quick Review Path

For a fast read, start here:

1. Read this first screen and the safety model above.
2. Skim [docs/architecture.md](docs/architecture.md) for the data flow and
   active-control gates.
3. Inspect `property_environment_manager/src/ventilation_manager/controller.py`
   and `property_environment_manager/src/trv_regulator/controller.py` for the
   decision state machines.
4. Check `tests/` for the humidity, TRV, calendar-policy, event-store, migration,
   and sanitisation cases.
5. Review [docs/privacy-and-safety.md](docs/privacy-and-safety.md) before looking
   at any logs or examples.

## Repository Shape

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
demo/
  status.json                 synthetic combined API payload
  dashboard-synthetic.jpg     screenshot generated from synthetic data
docs/
  architecture.md
  privacy-and-safety.md
tools/
  migrate_legacy_logs.py      imports old observer SQLite logs
  sanitize_sqlite.py          creates redacted SQLite copies
tests/
```

The combined runner deliberately avoids merging the controller internals. Each
subsystem keeps its own configuration, runtime state, database, tests, and
active-control switch, while the add-on process coordinates them under one
dashboard.

## Development

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e "property_environment_manager[dev]"
python3 -m pytest -q
python3 -m compileall -q property_environment_manager/src tests tools
```

The project targets Python 3.12+ because the Home Assistant add-on image uses
Python 3.12.

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
are retained longer so staged rollout decisions can be reviewed over time.

The TRV status includes one aggregate boiler decision every poll. A known
heating demand can recommend turning the boiler on, but turning it off is only
considered safe when the boiler and every configured TRV report usable state.
All future device commands use state read-back and bounded retries.

## Data Hygiene

Do not commit raw production databases. To preserve history from the old
standalone add-ons, copy the old SQLite files to a local working directory or to
`/share`, then import them into the new database files:

```bash
python3 tools/migrate_legacy_logs.py \
  --source old_ventilation_manager_events.sqlite3 \
  --destination new_ventilation_manager_events.sqlite3

python3 tools/migrate_legacy_logs.py \
  --source old_trv_regulator_events.sqlite3 \
  --destination new_trv_regulator_events.sqlite3
```

For public examples or external review, create redacted copies:

```bash
python3 tools/sanitize_sqlite.py \
  --source ventilation_manager_events.sqlite3 \
  --destination sanitized_ventilation.sqlite3
```

The sanitizer removes or generalises common private fields such as booking
summaries, guest names, bearer tokens, IP addresses, and house-specific entity
prefixes. Sanitized output should still be reviewed before publishing.

## Agent-Assisted Workflow

I use coding agents as part of the workflow for review, refactoring pressure
tests, documentation passes, and checklist-style verification. The product
boundaries, safety model, staged rollout, and final architecture decisions remain
mine. In this repo, the important signal is not "AI built it"; it is that the
automation is observable enough for humans and agents to inspect before it is
trusted.

## Public Boundary

Some tests and sanitisation rules refer to legacy numeric house/entity patterns
because the add-on was extracted from a real deployment. The demo artifacts use
a synthetic namespace, and any real logs should be sanitised and manually
reviewed before sharing.
