from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "home_assistant" / "device_health_package.yaml.tmpl"
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")


def render(house: str) -> str:
    return TEMPLATE.replace("__HOUSE__", house)


def automation(rendered: str, automation_id: str) -> str:
    marker = f'  - id: "{automation_id}"'
    start = rendered.index(marker)
    end = rendered.find('\n  - id: "', start + len(marker))
    return rendered[start:] if end == -1 else rendered[start:end]


@pytest.mark.parametrize("house", ["193", "195"])
def test_package_renders_stable_concise_webhook_incidents(house: str) -> None:
    rendered = render(house)

    assert "__HOUSE__" not in rendered
    assert "notify.notify" not in rendered
    assert rendered.count("action: rest_command.vbr_device_health") == 8

    expected_keys = {
        f"device-health:{house}:boiler": 2,
        f"device-health:{house}:ventilation-manager": 2,
        f"device-health:{house}:trv-manager": 2,
        f"device-health:{house}:lighting": 2,
    }
    for incident_key, count in expected_keys.items():
        assert rendered.count(f'incident_key: "{incident_key}"') == count

    literal_titles = re.findall(r'^\s+title: "([^"]+)"$', rendered, re.MULTILINE)
    literal_bodies = re.findall(r'^\s+body: "([^"]+)"$', rendered, re.MULTILINE)
    assert literal_titles
    assert literal_bodies
    assert all(len(title) <= 45 for title in literal_titles)
    assert all(len(body) <= 60 and "\n" not in body for body in literal_bodies)
    assert "friendly_name" not in rendered
    assert "daily_summary" not in rendered


@pytest.mark.parametrize("house", ["193", "195"])
def test_boiler_alert_and_recovery_are_independent_and_delayed(house: str) -> None:
    rendered = render(house)
    open_block = automation(rendered, f"device_health_{house}_boiler_open")
    recovered_block = automation(rendered, f"device_health_{house}_boiler_recovered")

    assert f"binary_sensor.{house}_boiler_relay_problem" in open_block
    assert 'to: "on"' in open_block
    assert 'for: "00:10:00"' in open_block
    assert "event: start" in open_block
    assert "trigger.id == 'startup'" in open_block
    assert open_block.index("- delay:") < open_block.index("- condition: state")
    assert open_block.index("- condition: state") < open_block.index(
        "action: rest_command.vbr_device_health"
    )
    assert 'state: "open"' in open_block
    assert 'severity: "urgent"' in open_block
    assert "notify: true" in open_block

    assert f"binary_sensor.{house}_boiler_relay_problem" in recovered_block
    assert 'from: "on"' in recovered_block
    assert 'to: "off"' in recovered_block
    assert ">= 600" in recovered_block
    assert 'state: "recovered"' in recovered_block
    assert f"switch.{house}_y_boiler" in recovered_block


@pytest.mark.parametrize("house", ["193", "195"])
def test_manager_incidents_are_separate_and_missing_entities_are_ignored(
    house: str,
) -> None:
    rendered = render(house)

    assert rendered.count("matches | count == 0") == 2
    assert rendered.count("> 300") == 2
    assert rendered.count("['control_error', 'unavailable', 'unknown']") == 2

    ventilation_open = automation(
        rendered, f"device_health_{house}_ventilation_manager_open"
    )
    ventilation_recovered = automation(
        rendered, f"device_health_{house}_ventilation_manager_recovered"
    )
    trv_open = automation(rendered, f"device_health_{house}_trv_manager_open")
    trv_recovered = automation(
        rendered, f"device_health_{house}_trv_manager_recovered"
    )

    assert f"sensor.{house}_ventilation_manager_health" in ventilation_open
    assert 'state: "open"' in ventilation_open
    assert 'for: "00:05:00"' in ventilation_open
    assert "event: start" in ventilation_open
    assert ventilation_open.index("- condition: state") < ventilation_open.index(
        "action: rest_command.vbr_device_health"
    )
    assert 'state: "recovered"' in ventilation_recovered
    assert ">= 300" in ventilation_recovered
    assert f"sensor.{house}_trv_regulator_health" in trv_open
    assert 'state: "open"' in trv_open
    assert 'for: "00:05:00"' in trv_open
    assert "event: start" in trv_open
    assert trv_open.index("- condition: state") < trv_open.index(
        "action: rest_command.vbr_device_health"
    )
    assert 'state: "recovered"' in trv_recovered
    assert ">= 300" in trv_recovered


@pytest.mark.parametrize("house", ["193", "195"])
def test_lighting_is_a_quiet_grouped_todo_after_each_lights_own_48_hours(
    house: str,
) -> None:
    rendered = render(house)
    lighting = automation(rendered, f"device_health_{house}_lighting_todo")

    assert "for item in states.light" in rendered
    assert "item.last_changed" in rendered
    assert ">= 172800" in rendered
    assert "problem_entities" in lighting
    assert 'severity: "advisory"' in lighting
    assert "notify: false" in lighting
    assert "'open' if trigger.to_state.state == 'on' else 'recovered'" in lighting
    startup = automation(rendered, f"device_health_{house}_lighting_startup_sync")
    assert "event: start" in startup
    assert 'delay: "00:01:00"' in startup
    assert f"binary_sensor.{house}_long_offline_lights" in startup
    assert "notify: false" in startup


def test_low_value_availability_entities_are_not_monitored() -> None:
    forbidden = (
        "_node_status",
        "_leak_water_leak",
        "_button_fan_battery",
        "_presence_presence",
        "_front_doorbell",
    )

    for suffix in forbidden:
        assert suffix not in TEMPLATE
    assert "states.lock" not in TEMPLATE
    assert "states.climate" not in TEMPLATE
    assert "states.camera" not in TEMPLATE


def test_automation_ids_are_unique() -> None:
    ids = re.findall(r'^  - id: "([^"]+)"$', TEMPLATE, re.MULTILINE)

    assert len(ids) == 8
    assert len(ids) == len(set(ids))
