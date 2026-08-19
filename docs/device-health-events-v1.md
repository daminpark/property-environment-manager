# Home Assistant device-health events v1

`home_assistant/device_health_events_package_v1.yaml.tmpl` carries the
operational events that remain useful after availability notifications are
trimmed. It uses the same authenticated `rest_command.vbr_device_health`
contract documented in `docs/device-health.md`; it never calls
`notify.notify`.

## Policy

| Event | Delay | Owner result | Recovery |
| --- | ---: | --- | --- |
| A or B water sensor reports an actual leak | None | Urgent push and durable owner task | Manual; each leak is a separate episode |
| Zigbee2MQTT emits `device_leave` with an IEEE address | None | Owner push and one durable task for that device; pairing opens for 120 seconds | A successful interview closes the same task quietly |
| Zigbee2MQTT reports a failed device interview | None | Urgent owner push and update to that device's task | A successful interview closes the same task quietly |
| A/B/C/K humidity reading is stale while its fan runs | 60 minutes | One grouped owner task, no push | Task closes when all affected sensors report or their fans stop |
| A/B/C/K fan runs continuously | 120 minutes | One owner task per fan, no push | That fan's task closes when it turns off |

Leak keys include both the entity ID and the episode start timestamp. A task
left open from an earlier incident therefore cannot suppress a later leak.
The package audits both leak sensors one minute after Home Assistant starts, so
an already-active leak is not missed during a restart. The audit reuses the
same episode key and therefore cannot duplicate a state-change alert.
Zigbee lifecycle keys use a stable SHA-256 token derived locally from the
normalized IEEE address and `vbr_device_health_incident_salt`. Leave, failed
interview, and successful interview events for one device therefore use the
same key, while simultaneous incidents for different devices remain isolated.
Only that opaque token leaves Home Assistant. The raw IEEE address, salt,
device name, and entity IDs remain local, and the webhook always receives an
empty `entity_ids` list for these events. The structured `incident_kind` field
distinguishes departure and pairing-failure phases without exporting device
identity. Do not rotate the salt while Zigbee incidents are open, because doing
so changes their keys.

Zigbee2MQTT lights are excluded from the generic 48-hour lighting incident;
their explicit lifecycle events are the only device-departure notification
path. Other MQTT lights remain eligible for the generic lighting policy.
Every incident key begins with `device-health:__HOUSE__:` and contains only
backend-safe key characters after rendering.

Humidity freshness prefers Home Assistant's `last_reported` timestamp and
falls back to `last_updated` on versions that do not expose it. The task is
grouped across all four humidity-controlled fans; C is intentionally included
even though the old notification omitted it.

Low batteries are intentionally absent. The existing `battery_monitor` package
already creates VBR system tasks, so adding them here would duplicate work and
could create a second notification path.

## Install

1. Render `__HOUSE__` as `193` or `195` and copy the result into that Home
   Assistant instance's `packages` directory.
2. Add a stable, independently generated random value named
   `vbr_device_health_incident_salt` to that instance's `secrets.yaml`. Keep it
   local to Home Assistant and out of source control.
3. Disable or remove the old `Notify on Zigbee Device Unpaired`, `Water Leak
   Alert`, fan-staleness, and fan-long-run automations before enabling this
   package. Otherwise both the old generic notification and the owner-only
   Tachbrook incident will fire.
4. Keep the existing Zigbee2MQTT bridge event topic and MQTT permissions. The
   package republishes permit-join with `{"value": true, "time": 120}` after a
   definitive leave.
5. Run Home Assistant's configuration check, restart or reload the relevant
   integrations, and verify the automations are enabled. Use a non-notifying
   test incident as described in `docs/device-health.md`; do not simulate a
   water leak or device leave merely to test push routing.
