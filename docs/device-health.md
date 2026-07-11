# Home Assistant device health

`home_assistant/device_health_package.yaml.tmpl` turns the small set of device
failures that merit owner attention into structured Tachbrook incidents. It
does not call Home Assistant's generic notification service. Home Assistant
reports incident state to Tachbrook, and the Tachbrook backend is responsible
for showing the owner-only to-do and deciding whether to send a native push.

## Alert policy

| Signal | Delay | Tachbrook result |
| --- | ---: | --- |
| Boiler relay unavailable or unknown | 10 continuous minutes | Urgent owner push and owner to-do |
| Enabled ventilation-manager error | 5 continuous minutes | Urgent owner push and owner to-do |
| Enabled ventilation-manager heartbeat | Stale for more than 5 minutes, then unhealthy for 5 minutes | Urgent owner push and owner to-do |
| Enabled TRV-manager error | 5 continuous minutes | Urgent owner push and owner to-do |
| Enabled TRV-manager heartbeat | Stale for more than 5 minutes, then unhealthy for 5 minutes | Urgent owner push and owner to-do |
| Individual lights unavailable or unknown | 48 continuous hours per light | One grouped owner to-do, no push |

Each boiler or manager incident has its own stable key and resolves
independently. A persistent lock or leak-monitor fault therefore cannot hide a
boiler recovery. Recoveries use the same key as their open incident. Boiler and
manager recoveries produce a short owner push; lighting recovery only closes
the quiet to-do.

A manager is considered enabled when its health entity exists. `control_error`,
`unavailable`, and `unknown` are errors. The manager's `blocked` state is not an
alert by itself because it commonly reflects devices intentionally excluded
from this policy. A health entity that stops reporting is still detected even
if its visible state remains `ok`.

Locks, lock node-status sensors, leak-monitor availability, fan-button battery
entities, presence sensors, TRVs, fans, thermometers, and doorbells do not
create device-health pushes or to-dos. Other automations can continue to report
actual leaks, access events, or other domain events; this package only governs
availability noise.

The lighting list only considers individual `light.__HOUSE___*` entities.
Light groups are excluded. Eligibility is based on each entity's own
`last_changed`, so one recently failed light cannot inherit another light's
48-hour timer. The grouped incident is updated whenever the eligible set
changes and recovered when it becomes empty.

## Configure the Tachbrook webhook

Define the REST command once in Home Assistant's `configuration.yaml`. Keep the
shared secret in `secrets.yaml`.

```yaml
rest_command:
  vbr_device_health:
    url: "https://YOUR-TACHBROOK-HOST/api/webhooks/ha/device-health"
    method: POST
    headers:
      x-webhook-secret: !secret vbr_ha_webhook_secret
    content_type: "application/json"
    timeout: 15
    payload: >-
      {
        "incident_key": {{ incident_key | to_json }},
        "house": {{ house | to_json }},
        "state": {{ state | to_json }},
        "observed_at": {{ observed_at | default(now().isoformat(), true) | to_json }},
        "severity": {{ severity | to_json }},
        "notify": {{ notify | to_json }},
        "title": {{ title | to_json }},
        "body": {{ body | to_json }},
        "entity_ids": {{ entity_ids | default([]) | to_json }}
      }
```

```yaml
# secrets.yaml
vbr_ha_webhook_secret: "replace-with-the-same-secret-used-by-tachbrook"
```

Tachbrook must authenticate this endpoint, upsert by `incident_key`, and apply
owner-only access at both the push and to-do layers. It must not infer owner
routing from a Home Assistant notification target, and delegates must not be
able to list or open these incidents.

## Install and verify

Render one copy per Home Assistant instance, replacing every `__HOUSE__` token
with `193` or `195`, and put the result in that instance's packages directory.
Then run Home Assistant's configuration check and reload template entities and
automations (or restart Home Assistant).

Before enabling the automations, call `rest_command.vbr_device_health` manually
with `notify: false` and a temporary incident key to verify authentication and
payload handling without sending a push. No `notify.notify` service or mobile
device target is required by this package.
