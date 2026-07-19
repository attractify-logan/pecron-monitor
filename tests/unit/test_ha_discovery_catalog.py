"""Golden characterization of Home Assistant MQTT discovery catalogs."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ha_bridge import HomeAssistantBridge


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "ha_discovery" / "catalog.json"
CATALOG = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

BASE_CONTROLS = {
    "battery_percentage",
    "voltage",
    "total_input_power",
    "total_output_power",
    "ac_switch_hm",
    "dc_switch_hm",
    "ups_status_hm",
}
ALL_CAPABILITY_CONTROLS = BASE_CONTROLS | {
    "battery_temp",
    "charging_plate_temp",
    "inverter_temp",
    "eco_quite_mode_as",
    "device_touch_locking_as",
    "ac_charging_power_ios",
    "ups_start_charge_value_as",
    "device_standy_times_as",
    "total_energy",
}
TSL_GATED_REFS = {
    "select/ac_charging_power",
    "select/ups_charge_threshold",
    "sensor/battery_temp",
    "sensor/charging_plate_temp",
    "sensor/inverter_temp",
    "sensor/standby_timeout",
    "sensor/total_energy",
    "switch/eco_mode",
    "switch/touch_lock",
}
DEFERRED_PORT_REFS = {
    f"sensor/{port}_input_{measurement}"
    for port in ("dc5521", "gx16mf1", "gx16mf2")
    for measurement in ("voltage", "current", "power")
}

CASES = (
    (
        "all_capabilities",
        "E3800-CATALOG",
        "E3800LFP",
        ALL_CAPABILITY_CONTROLS,
        [
            "pecron/E3800-CATALOG/ac/set",
            "pecron/E3800-CATALOG/dc/set",
            "pecron/E3800-CATALOG/ups/set",
            "pecron/E3800-CATALOG/eco_mode/set",
            "pecron/E3800-CATALOG/touch_lock/set",
            "pecron/E3800-CATALOG/ac_charging_power/set",
            "pecron/E3800-CATALOG/ups_charge_threshold/set",
            "pecron/E3800-CATALOG/auto_light_flag_as/set",
        ],
    ),
    (
        "minimal",
        "E1500-CATALOG",
        "E1500LFP",
        BASE_CONTROLS,
        [
            "pecron/E1500-CATALOG/ac/set",
            "pecron/E1500-CATALOG/dc/set",
            "pecron/E1500-CATALOG/ups/set",
            "pecron/E1500-CATALOG/auto_light_flag_as/set",
        ],
    ),
)


def _device(device_key, model, controls):
    return {
        "device_key": device_key,
        "device_name": model,
        "controls": {control: {} for control in sorted(controls)},
    }


def _bridge(device, *, clear=False):
    bridge = HomeAssistantBridge(
        {
            "discovery_prefix": "homeassistant",
            "clear_discovery_on_startup": clear,
        },
        [device],
    )
    bridge.client = MagicMock()
    bridge._connected = True
    return bridge


def _topic(ref, device_key):
    component, key = ref.split("/", 1)
    return f"homeassistant/{component}/pecron_{device_key}/{key}/config"


def _ref(topic, device_key):
    prefix = "homeassistant/"
    marker = f"/pecron_{device_key}/"
    assert topic.startswith(prefix) and topic.endswith("/config")
    component, key = topic[len(prefix) : -len("/config")].split(marker, 1)
    return f"{component}/{key}"


def _normalize_device_key(value, device_key):
    if isinstance(value, dict):
        return {key: _normalize_device_key(item, device_key) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_device_key(item, device_key) for item in value]
    if isinstance(value, str):
        return value.replace(device_key, "$DEVICE_KEY")
    return value


def _expected_device_info(device_key, model):
    return {
        "identifiers": [f"pecron_{device_key}"],
        "name": f"Pecron {model} ({device_key})",
        "manufacturer": "Pecron",
        "model": model,
        "serial_number": device_key,
    }


def _config_publishes(bridge):
    configs = {}
    empty_topics = []
    for call in bridge.client.publish.call_args_list:
        assert len(call.args) == 2
        assert call.kwargs == {"qos": 1, "retain": True}
        topic, raw_payload = call.args
        if raw_payload == "":
            empty_topics.append(topic)
        else:
            assert topic not in configs
            configs[topic] = json.loads(raw_payload)
    return configs, empty_topics


def _assert_golden_configs(configs, refs, device_key, model, expected_payloads):
    expected_topics = {_topic(ref, device_key) for ref in refs}
    assert set(configs) == expected_topics

    expected_device = _expected_device_info(device_key, model)
    for topic in sorted(expected_topics):
        ref = _ref(topic, device_key)
        payload = configs[topic]
        assert payload.pop("device") == expected_device, ref
        assert _normalize_device_key(payload, device_key) == expected_payloads[ref], ref


@pytest.mark.parametrize(
    ("catalog_name", "device_key", "model", "controls", "command_topics"),
    CASES,
    ids=("E3800 all capabilities", "E1500 minimal"),
)
def test_discovery_catalog_matches_exact_golden_payloads(
    catalog_name, device_key, model, controls, command_topics
):
    bridge = _bridge(_device(device_key, model, controls))

    bridge._publish_discovery()

    configs, empty_topics = _config_publishes(bridge)
    refs = set(CATALOG["catalogs"][catalog_name])
    expected_payloads = CATALOG["configs"]
    _assert_golden_configs(configs, refs, device_key, model, expected_payloads)

    assert set(_ref(topic, device_key) for topic in empty_topics) == set(
        CATALOG["stale"][catalog_name]
    )
    assert bridge._published_topics == {_topic(ref, device_key) for ref in refs}
    assert bridge._command_topics == command_topics
    assert len(bridge._command_topics) == len(set(bridge._command_topics))
    assert bridge._clear_current_discovery is False
    assert bridge._device_dev_info == {device_key: _expected_device_info(device_key, model)}
    assert bridge._deferred_ports_published == set()
    assert refs.isdisjoint(DEFERRED_PORT_REFS)


def test_tsl_capabilities_are_the_only_catalog_difference():
    all_capabilities = set(CATALOG["catalogs"]["all_capabilities"])
    minimal = set(CATALOG["catalogs"]["minimal"])

    assert all_capabilities - minimal == TSL_GATED_REFS
    assert minimal - all_capabilities == set()


def test_clear_then_republish_is_exact_and_resets_bookkeeping_each_pass():
    catalog_name, device_key, model, controls, command_topics = CASES[0]
    bridge = _bridge(_device(device_key, model, controls), clear=True)
    expected_refs = set(CATALOG["catalogs"][catalog_name])
    expected_current_topics = {_topic(ref, device_key) for ref in expected_refs}
    expected_stale_topics = {_topic(ref, device_key) for ref in CATALOG["stale"][catalog_name]}

    for _ in range(2):
        bridge.client.reset_mock()
        bridge._publish_discovery()
        calls_by_topic = {}
        for call in bridge.client.publish.call_args_list:
            assert call.kwargs == {"qos": 1, "retain": True}
            calls_by_topic.setdefault(call.args[0], []).append(call.args[1])

        assert set(calls_by_topic) == expected_current_topics | expected_stale_topics
        for topic in expected_current_topics:
            payloads = calls_by_topic[topic]
            assert len(payloads) == 2
            assert payloads[0] == ""
            assert json.loads(payloads[1])["unique_id"].startswith(f"pecron_{device_key}_")
        for topic in expected_stale_topics:
            assert calls_by_topic[topic] == [""]

        assert bridge._published_topics == expected_current_topics
        assert bridge._command_topics == command_topics
        assert bridge._clear_current_discovery is False


def test_dc_input_ports_are_omitted_until_each_port_is_registered_once():
    _, device_key, model, controls, _ = CASES[0]
    bridge = _bridge(_device(device_key, model, controls))
    bridge._publish_discovery()
    initial_configs, _ = _config_publishes(bridge)
    assert set(_ref(topic, device_key) for topic in initial_configs).isdisjoint(DEFERRED_PORT_REFS)

    bridge.client.reset_mock()
    for port in ("dc5521", "gx16mf1", "gx16mf2"):
        bridge._ensure_port_discovery(device_key, port)

    configs, empty_topics = _config_publishes(bridge)
    assert empty_topics == []
    _assert_golden_configs(
        configs,
        DEFERRED_PORT_REFS,
        device_key,
        model,
        CATALOG["deferred_ports"],
    )
    assert bridge._deferred_ports_published == {
        (device_key, "dc5521"),
        (device_key, "gx16mf1"),
        (device_key, "gx16mf2"),
    }

    bridge.client.reset_mock()
    for port in ("dc5521", "gx16mf1", "gx16mf2"):
        bridge._ensure_port_discovery(device_key, port)
    bridge.client.publish.assert_not_called()
