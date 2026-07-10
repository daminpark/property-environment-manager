# Home Assistant device health

`home_assistant/device_health_package.yaml.tmpl` is a notification-only Home
Assistant package. Replace `__HOUSE__` with the local house code, copy the
rendered file into Home Assistant's `packages` directory, run a configuration
check, and reload template entities and automations.

The package separates alert urgency by device role:

- environment-manager control errors or missing heartbeats: 5 minutes;
- locks, TRVs, fans, boiler relays, leak sensors, and doorbells: 10 minutes;
- thermometers, presence sensors, and fan buttons: 15 minutes;
- lights: one daily aggregate because wall switches can intentionally remove
  power from individual bulbs.

Every immediate category has a recovery notification. The daily summary is
bounded so a Zigbee bridge outage cannot create an unbounded notification.
Delayed startup audits cover devices that were already unavailable before Home
Assistant attached the state-change automations, without duplicating alerts.
Zigbee2MQTT availability must be enabled; passive-device timeout policy remains
owned by Zigbee2MQTT so sparse battery sensors are not inferred stale from an
unchanged temperature value.
