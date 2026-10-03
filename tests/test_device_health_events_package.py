from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    ROOT / "home_assistant" / "device_health_events_package_v1.yaml.tmpl"
)
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")


def render(house: str) -> str:
    return TEMPLATE.replace("__HOUSE__", house)


def automation(rendered: str, automation_id: str) -> str:
    marker = f'  - id: "{automation_id}"'
    start = rendered.index(marker)
    end = rendered.find('\n  - id: "', start + len(marker))
    return rendered[start:] if end == -1 else rendered[start:end]


@pytest.mark.parametrize("house", ["193", "195"])
def test_v1_renders_only_structured_owner_incidents(house: str) -> None:
    rendered = render(house)

    assert "__HOUSE__" not in rendered
    assert "notify.notify" not in rendered
    assert "rest_command.vbr_system_task" not in rendered
    assert rendered.count("action: rest_command.vbr_device_health") == 8
    assert rendered.count(f"device-health:{house}:") == 6
    assert set(re.findall(r'^\s+state: "(open|recovered)"$', rendered, re.MULTILINE)) == {
        "open",
        "recovered",
    }
    assert set(re.findall(r'^\s+severity: "([^"]+)"$', rendered, re.MULTILINE)) == {
        "urgent",
        "advisory",
    }

    representative_keys = (
        f"device-health:{house}:water-leak:binary_sensor.{house}_a_leak_water_leak:1783700000",
        f"device-health:{house}:zigbee:{'a' * 64}",
        f"device-health:{house}:fan-humidity-stale",
        f"device-health:{house}:fan-long-run:a",
    )
    assert all(KEY_RE.fullmatch(key) for key in representative_keys)


@pytest.mark.parametrize("house", ["193", "195"])
def test_water_leaks_are_immediate_unique_durable_episodes(house: str) -> None:
    rendered = render(house)
    leak = automation(rendered, f"device_health_events_v1_{house}_water_leak")

    assert f"binary_sensor.{house}_a_leak_water_leak" in leak
    assert f"binary_sensor.{house}_b_leak_water_leak" in leak
    assert 'to: "on"' in leak
    assert "for:" not in leak
    assert "trigger.to_state.last_changed" in leak
    assert "{{ trigger.entity_id }}:{{ episode_started }}" in leak
    assert 'state: "open"' in leak
    assert 'severity: "urgent"' in leak
    assert "notify: true" in leak
    assert 'state: "recovered"' not in leak
    assert "mode: queued" in leak

    startup = automation(
        rendered, f"device_health_events_v1_{house}_water_leak_startup"
    )
    assert "event: start" in startup
    assert 'delay: "00:01:00"' in startup
    assert "is_state(repeat.item, 'on')" in startup
    assert f"binary_sensor.{house}_a_leak_water_leak" in startup
    assert f"binary_sensor.{house}_b_leak_water_leak" in startup
    assert "states[repeat.item].last_changed" in startup
    assert "notify: true" in startup


@pytest.mark.parametrize("house", ["193", "195"])
def test_zigbee_lifecycle_is_private_stable_and_keeps_pairing_window(
    house: str,
) -> None:
    rendered = render(house)
    lifecycle = automation(
        rendered, f"device_health_events_v1_{house}_zigbee_lifecycle"
    )

    assert "zigbee2mqtt/bridge/event" in lifecycle
    assert "event_type == 'device_leave'" in lifecycle
    assert "interview_status == 'failed'" in lifecycle
    assert "interview_status == 'successful'" in lifecycle
    assert "vbr_device_health_incident_salt" in lifecycle
    assert "(incident_salt ~ ':' ~ ieee_address) | sha256" in lifecycle
    assert f"device-health:{house}:zigbee:" in lifecycle
    assert lifecycle.count('incident_key: "{{ incident_key }}"') == 3
    assert "zigbee2mqtt/bridge/request/permit_join" in lifecycle
    assert "{\"value\": true, \"time\": 120}" in lifecycle
    assert lifecycle.index("action: mqtt.publish") < lifecycle.index(
        "action: rest_command.vbr_device_health"
    )
    assert 'state: "open"' in lifecycle
    assert 'state: "recovered"' in lifecycle
    assert 'severity: "urgent"' in lifecycle
    assert 'severity: "advisory"' in lifecycle
    assert lifecycle.count("notify: true") == 2
    assert lifecycle.count("notify: false") == 1
    assert lifecycle.count("entity_ids: []") == 3
    assert lifecycle.count('incident_kind: "zigbee_departure"') == 2
    assert lifecycle.count('incident_kind: "zigbee_pairing_failed"') == 1
    # The owner sees which device left and how often it was power-cycled
    # first (bulbs reset after a few quick off/on cycles). The IEEE address
    # stays local: it only feeds the salted key and the power-on lookup.
    assert "data.get('friendly_name', '')" in lifecycle
    assert "{{ device_name if device_name else 'A Zigbee device' }}" in lifecycle
    assert f"'{house} Zigbee device left: ' ~ device_name if device_name" in lifecycle
    assert "It powered on {{ power_ons }}" in lifecycle
    assert f"state_attr('sensor.{house}_zigbee_power_ons_v1', 'recent')" in lifecycle
    for body in re.findall(r"body: >-\n((?:\s{20}.*\n)+)", lifecycle):
        assert "ieee" not in body


@pytest.mark.parametrize("house", ["193", "195"])
def test_running_fan_staleness_is_grouped_quiet_and_closes(house: str) -> None:
    rendered = render(house)
    stale = automation(
        rendered, f"device_health_events_v1_{house}_fan_humidity_stale"
    )

    for zone in "abck":
        assert f"sensor.{house}_{zone}_thermometer_humidity" in rendered
        assert f"switch.{house}_{zone}_fan" in rendered
    assert "item.last_reported" in rendered
    assert "else item.last_updated" in rendered
    assert ">= 3600" in rendered
    assert 'minutes: "/5"' in rendered
    assert f'device-health:{house}:fan-humidity-stale' in stale
    assert "problem_entities" in stale
    assert "'open' if trigger.to_state.state == 'on' else 'recovered'" in stale
    assert 'severity: "advisory"' in stale
    assert "notify: false" in stale


@pytest.mark.parametrize("house", ["193", "195"])
def test_long_run_tasks_are_per_fan_quiet_and_recover_independently(house: str) -> None:
    rendered = render(house)
    opened = automation(
        rendered, f"device_health_events_v1_{house}_fan_long_run_open"
    )
    recovered = automation(
        rendered, f"device_health_events_v1_{house}_fan_long_run_recovered"
    )

    for zone in "abck":
        assert f"switch.{house}_{zone}_fan" in opened
        assert f"switch.{house}_{zone}_fan" in recovered
    assert 'for: "02:00:00"' in opened
    assert f"device-health:{house}:fan-long-run:{{{{ fan_zone }}}}" in opened
    assert 'state: "open"' in opened
    assert "notify: false" in opened
    assert 'from: "on"' in recovered
    assert 'to: "off"' in recovered
    assert ">= 7200" in recovered
    assert f"device-health:{house}:fan-long-run:{{{{ fan_zone }}}}" in recovered
    assert 'state: "recovered"' in recovered
    assert "notify: false" in recovered


def test_v1_automation_ids_are_unique_and_battery_is_not_duplicated() -> None:
    ids = re.findall(r'^  - id: "([^"]+)"$', TEMPLATE, re.MULTILINE)

    assert len(ids) == 6
    assert len(ids) == len(set(ids))
    assert "_battery" not in TEMPLATE
    assert "battery_low" not in TEMPLATE
    assert "rest_command.vbr_system_task" not in TEMPLATE


@pytest.mark.parametrize("house", ["193", "195"])
def test_power_on_announcements_are_kept_for_ten_minutes_per_device(house: str) -> None:
    rendered = render(house)
    start = rendered.index(f"{house} Zigbee Power-ons V1")
    sensor = rendered[start:rendered.index("  - triggers:", start)]
    assert rendered.index("topic: zigbee2mqtt/bridge/event") < start
    assert f'unique_id: "device_health_events_v1_{house}_zigbee_power_ons"' in sensor
    assert "payload.get('type', '') == 'device_announce'" in sensor
    assert "as_timestamp(now()) - 600" in sensor
    assert "this.attributes.get('recent', {})" in sensor
    assert "rest_command" not in sensor and "notify" not in sensor
