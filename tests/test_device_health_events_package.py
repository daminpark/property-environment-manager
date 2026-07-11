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
    assert rendered.count("action: rest_command.vbr_device_health") == 6
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
        f"device-health:{house}:zigbee-leave:0xa4c1381234567890:1783700000",
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
def test_device_leave_is_definitive_and_keeps_pairing_window(house: str) -> None:
    rendered = render(house)
    leave = automation(rendered, f"device_health_events_v1_{house}_zigbee_leave")

    assert "zigbee2mqtt/bridge/event" in leave
    assert "payload.get('type') == 'device_leave'" in leave
    assert "data.get('ieee_address', '')" in leave
    assert "regex_replace('[^a-z0-9._:-]', '-')" in leave
    assert (
        f"device-health:{house}:zigbee-leave:{{{{ device_key }}}}:"
        "{{ episode_started }}"
    ) in leave
    assert "as_timestamp(now(), 0) | int" in leave
    assert "zigbee2mqtt/bridge/request/permit_join" in leave
    assert "{\"value\": true, \"time\": 120}" in leave
    assert leave.index("action: mqtt.publish") < leave.index(
        "action: rest_command.vbr_device_health"
    )
    assert 'severity: "urgent"' in leave
    assert "notify: true" in leave


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
