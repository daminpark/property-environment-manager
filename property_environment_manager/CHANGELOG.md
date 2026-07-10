# Changelog

## Unreleased

## 0.2.1 - 2026-07-10

- Adds a reusable Home Assistant device-health package for aggregated outage,
  manager-heartbeat, recovery, and daily lighting notifications.
- Adds delayed startup audits so pre-existing outages cannot be missed while
  Home Assistant initializes template entities and automations.
- Improves public portfolio documentation, privacy notes, and architecture notes.
- Adds synthetic demo artifacts, CI checks, and reproducible dev dependencies.
- Keeps demo-safe examples separate from real runtime defaults.
- Blocks drying-room writes from stale inputs and restores temporary boosts after
  humidity recovery.
- Preserves ventilation events through sensor outages and reliably stops fans
  started before a baseline was available.
- Reconciles missed calendar transitions once without overriding later guest
  adjustments.
- Removes calendar titles from diagnostics, redacts legacy SQLite and runtime
  state data, and escapes dashboard data before HTML rendering.
- Updates source-build metadata for current Home Assistant Supervisor releases.
- Isolates controller failures per zone so diagnostics or device errors do not
  starve later rooms.

## 0.2.0

- Adds fail-safe aggregate boiler demand diagnostics on every TRV poll.
- Adds a separate boiler-control gate; boiler turn-off is blocked when any TRV
  demand is unknown.
- Adds verified state read-back and bounded retry support for future device
  writes.
- Makes ventilation observer minimum runtime independent of the real fan state.
- Suppresses low-delta rate-only humidity starts and treats unavailable fans as
  hard write blockers.
- Uses source humidity timestamps for sparse/change-only sensors.
- Removes the obsolete ventilation maximum-runtime option.
- Documents staged ownership of legacy presence, button, air-out, drying-room,
  renovation, and calendar routines.
- Imports legacy standalone SQLite databases transactionally on first start,
  validates them, and carries over controller runtime state without overwriting
  existing combined data.

## 0.1.0

- Initial combined Home Assistant add-on.
- Ports Ventilation Manager and TRV Regulator into one repository.
- Adds a combined ingress dashboard and status API.
- Keeps ventilation, TRV, and TRV calendar-policy writes separately gated.
- Adds legacy SQLite migration and sanitization tools.
