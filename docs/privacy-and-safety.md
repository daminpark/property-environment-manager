# Privacy And Safety

This repository comes from a real property-operations automation context. The
public repo should be useful to review without exposing a real property,
guest/booking data, tokens, IP addresses, or Home Assistant entity namespace.

## Safety Model

The add-on is observer-first by default:

- ventilation writes are disabled unless `vent_active_control` is true;
- TRV drying-room writes are disabled unless `trv_active_control` is true;
- boiler writes require both `trv_active_control` and
  `trv_active_boiler_control`;
- TRV calendar, guest-limit, service-default, force-heat, and child-lock writes
  also require `trv_active_calendar_policy` to be true.

The intent is staged rollout: observe decisions, compare against the current
system, review logs and daily summaries, then enable only the narrow write path
that has earned trust.

## Privacy Boundary

Do not commit:

- raw SQLite databases from `/data`;
- booking or guest names;
- Home Assistant long-lived access tokens;
- IP addresses or local network details;
- screenshots that show real entity names, calendars, people, or addresses.

The repo includes `tools/sanitize_sqlite.py` for redacted copies, but sanitized
output still needs a human review before publishing.

At runtime, calendar titles are used only to ignore blocked events. They are not
copied into Home Assistant diagnostics, status APIs, controller logs, or SQLite
event payloads; calendar transitions use opaque identifiers instead.
On upgrade, the add-on redacts titles from its existing TRV SQLite event and
sample rows and removes legacy diagnostic reasons from its runtime state file.
Historical attributes already retained by Home Assistant Recorder remain subject
to the deployment's Recorder retention and purge policy.

## Public Examples

Only publish synthetic or sanitized artifacts. The `demo/` directory is safe for
public review because it uses fake house IDs, fake room names, synthetic metrics,
and no real calendar summaries.

## Private Deployment Conventions

Some tests and sanitisation rules contain legacy numeric entity patterns such as
`193` and `195`. They are retained so the migration and redaction tools can prove
they handle the real shapes they were built for. They should be read as local
deployment conventions, not as reusable product requirements.

## Responsible Operation

Before enabling active control in a real Home Assistant deployment:

1. Run in observer mode.
2. Review the dashboard and SQLite daily summaries.
3. Check stale-sensor and unavailable-entity behaviour.
4. Enable the smallest write gate needed.
5. Keep calendar-policy writes separate until parity is clear.
6. Never enable a new write gate while the matching legacy automation can still
   write the same policy.
7. Treat unavailable actuators, stale sensors, unknown TRV demand, and failed
   command read-back as hard rollout blockers.
