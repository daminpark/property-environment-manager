# Architecture

Property Environment Manager is one Home Assistant add-on process around two
independently tested controllers:

- `ventilation_manager` handles humidity-driven fan decisions.
- `trv_regulator` handles heating/TRV observations and recommendations.

The combined layer starts both controllers, exposes one dashboard, and keeps the
control gates separate. This keeps the first unified add-on small enough to
review while preserving the behaviour of the proven standalone modules.

## Data Flow

```text
Home Assistant states
        |
        v
controller snapshots
        |
        v
state-machine decisions
        |
        +--> Home Assistant diagnostic entities
        +--> read-only dashboard/API
        +--> local SQLite events, samples, and daily summaries
        +--> optional device writes when active-control gates are enabled
```

The controllers store a small amount of runtime state on disk so they can reason
about trends after restarts. SQLite logs are separate from Home Assistant
Recorder because the observer data is operational evidence, not just UI history.

## Ventilation Controller

The ventilation controller learns per-zone absolute-humidity baselines while the
room is stable. It starts or recommends fan runtime when humidity rises above
that baseline, when the rate of change is high, or when relative humidity crosses
a high-humidity guard. It avoids learning a baseline from already-wet rooms and
treats stale or change-only sensors conservatively.

Important review points:

- learned baseline and event baseline are separate;
- stale sensors do not silently create new starts from old readings;
- fans are only written when `vent_active_control` is enabled;
- observer mode still records mismatches between "fan is on" and "fan should run";
- observer minimum runtime is counterfactual and does not depend on the legacy
  automation turning the physical fan on;
- unavailable fans block writes, and rate-only starts also require a meaningful
  rise above the learned baseline;
- initial active scope is humidity only; presence, button, scheduled air-out,
  and drying-room routines remain explicitly Home Assistant-owned.

## TRV Controller

The TRV controller observes room temperature, TRV target, HVAC mode/action,
boiler state, humidity, child lock, and calendar events. It can recommend drying
room target increases, detect suspected open windows, flag ineffective heating,
mirror calendar check-in/check-out heating policy, enforce guest target bounds,
restore service-room defaults, restore heat mode, and restore child lock.

Important review points:

- drying-room writes only require `trv_active_control`;
- boiler writes require both `trv_active_control` and
  `trv_active_boiler_control`;
- calendar, guest-limit, service-default, force-heat, and child-lock writes also
  require `trv_active_calendar_policy`;
- calendar policy can be observed for parity before it is allowed to write;
- daily summaries distinguish safe improvement candidates from unsafe write
  blockers;
- aggregate boiler demand is recomputed every poll rather than only when a TRV
  changes state;
- a boiler turn-off is blocked if any TRV is unavailable or has unknown demand;
- device writes require state read-back with bounded retries.

## Why The Controllers Stay Separate

The combined add-on intentionally does not merge the controller internals. The
two domains have different failure modes, state, and rollout risk. Keeping them
separate makes code review simpler, preserves focused tests, and allows one
subsystem to remain observer-only while the other is active.

## Interfaces

- Home Assistant REST API for states, diagnostic state publishing, service calls,
  and calendar events.
- Add-on environment variables mapped from `config.yaml` by `run.sh`.
- Read-only HTTP dashboard/API exposed through Home Assistant ingress.
- SQLite files under `/data` for local event, sample, and daily-summary history.

## Deployment Defaults

Demo artifacts use a synthetic house namespace. A real deployment should review
`house_code`, zone IDs, timezone, and active-control flags explicitly in Home
Assistant add-on options.

## Staged Ownership

The observer and existing Home Assistant automations may run together because
the observer does not write. At cutover, only the legacy automation matching the
newly enabled write gate should be disabled. Unrelated safety and auxiliary
routines remain enabled until the add-on models and tests them explicitly.

Recommended order:

1. combined observer with every write gate off;
2. humidity control for one zone while presence/button/schedule routines remain;
3. calendar/TRV policy after event parity review;
4. boiler control only after relay availability and mismatch history are clean;
5. remove remaining legacy routines only after their behavior is represented.
