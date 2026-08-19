from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EVENTS_TEMPLATE_PATH = (
    ROOT / "home_assistant" / "device_health_events_package_v1.yaml.tmpl"
)
GENERIC_TEMPLATE_PATH = ROOT / "home_assistant" / "device_health_package.yaml.tmpl"
EVENTS_TEMPLATE = EVENTS_TEMPLATE_PATH.read_text(encoding="utf-8")
GENERIC_TEMPLATE = GENERIC_TEMPLATE_PATH.read_text(encoding="utf-8")


class HomeAssistantLoader(yaml.SafeLoader):
    pass


HomeAssistantLoader.add_constructor(
    "!secret", lambda loader, node: loader.construct_scalar(node)
)


def _load_events_package(house: str = "demo") -> dict:
    rendered = EVENTS_TEMPLATE.replace("__HOUSE__", house)
    return yaml.load(rendered, Loader=HomeAssistantLoader)


def _automation(source: str, automation_id: str) -> str:
    marker = f'  - id: "{automation_id}"'
    start = source.index(marker)
    end = source.find('\n  - id: "', start + len(marker))
    return source[start:] if end == -1 else source[start:end]


def _incident_key(salt: str, ieee_address: str, house: str = "demo") -> str:
    normalized = ieee_address.strip().lower()
    digest = hashlib.sha256(f"{salt}:{normalized}".encode()).hexdigest()
    return f"device-health:{house}:zigbee:{digest}"


def test_events_package_is_valid_yaml_and_uses_external_rest_command() -> None:
    package = _load_events_package()
    automation_ids = {item["id"] for item in package["automation"]}

    assert "device_health_events_v1_demo_zigbee_lifecycle" in automation_ids
    assert "rest_command" not in package
    assert "vbr_device_health_webhook_url" not in EVENTS_TEMPLATE
    assert "vbr_device_health_webhook_secret" not in EVENTS_TEMPLATE


def test_zigbee_incident_key_is_stable_and_isolated_per_device() -> None:
    salt = "stable-local-test-salt"
    first = _incident_key(salt, " 0X0000000000000001 ")
    first_again = _incident_key(salt, "0x0000000000000001")
    second = _incident_key(salt, "0x0000000000000002")

    assert first == first_again
    assert first != second
    assert len(first.rsplit(":", 1)[1]) == 64
    assert len(first) <= 160
    assert re.fullmatch(r"[a-z0-9][a-z0-9._:-]*", first)

    lifecycle = _automation(
        EVENTS_TEMPLATE,
        "device_health_events_v1___HOUSE___zigbee_lifecycle",
    )
    assert lifecycle.count("(incident_salt ~ ':' ~ ieee_address) | sha256") == 1
    assert lifecycle.count('incident_key: "{{ incident_key }}"') == 3
    assert "vbr_device_health_incident_salt" in lifecycle
    assert "ieee_address | length > 0" in lifecycle
    assert lifecycle.count("| string | trim | lower") == 3


def test_zigbee_event_fields_guard_missing_or_malformed_data() -> None:
    lifecycle = _automation(
        EVENTS_TEMPLATE,
        "device_health_events_v1___HOUSE___zigbee_lifecycle",
    )

    assert lifecycle.count("trigger.payload_json is defined") == 3
    assert lifecycle.count("trigger.payload_json is mapping") == 3
    assert lifecycle.count("raw_data if raw_data is mapping else {}") == 2
    assert "trigger.payload_json.type" not in lifecycle
    assert "trigger.payload_json.data" not in lifecycle
    assert "ieee_address | length > 0" in lifecycle


def test_zigbee_webhook_fields_are_structured_and_privacy_preserving() -> None:
    package = _load_events_package()
    lifecycle = next(
        item
        for item in package["automation"]
        if item["id"] == "device_health_events_v1_demo_zigbee_lifecycle"
    )
    choices = lifecycle["actions"][1]["choose"]

    assert len(choices) == 3
    assert [
        next(
            action["data"]["incident_kind"]
            for action in choice["sequence"]
            if action.get("action") == "rest_command.vbr_device_health"
        )
        for choice in choices
    ] == ["zigbee_departure", "zigbee_pairing_failed", "zigbee_departure"]
    for choice in choices:
        rest_actions = [
            action
            for action in choice["sequence"]
            if action.get("action") == "rest_command.vbr_device_health"
        ]
        assert len(rest_actions) == 1
        data = rest_actions[0]["data"]
        assert set(data) == {
            "incident_key",
            "house",
            "incident_kind",
            "state",
            "severity",
            "notify",
            "title",
            "body",
            "entity_ids",
        }
        assert data["incident_key"] == "{{ incident_key }}"
        assert data["entity_ids"] == []
        serialized = repr(data)
        assert "ieee_address" not in serialized
        assert "incident_salt" not in serialized
        assert "friendly_name" not in serialized
        assert "device_name" not in serialized
        assert "entity_id" not in serialized.replace("entity_ids", "")


def test_only_zigbee2mqtt_lights_leave_generic_availability_monitoring() -> None:
    source = GENERIC_TEMPLATE

    assert source.count("integration_entities('mqtt')") == 2
    assert source.count("and 'zigbee2mqtt' in (ids[1] | string | lower)") == 2
    assert source.count("item.entity_id not in z2m.entities") == 2
    assert "item.entity_id not in mqtt_entities" not in source
