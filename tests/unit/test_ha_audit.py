"""Home Assistant connection audit fixes (v0.8.4).

Each test pins a problem seen on a live install on 2026-10-21:

- switch/bypass was rejected by HA ("required key not provided @
  data['command_topic']") on every startup for both an E1500LFP and an E3800LFP.
- The select templates rendered '%' or failed when their field was missing
  from a state payload.
- 9,538 "'dict object' has no attribute 'add_bat_status_hm'" warnings in 48 h:
  neither unit ever reports that field.
- After each monitor restart the first state payload lacked most keys, so
  text sensors such as Device Status were set to an empty string.
- Every broker reconnect cleared and re-created every entity.
- Entities kept their last values forever when the monitor or the device
  stopped reporting, so an "unavailable for 15 min" alert could never fire.
"""

import json
from unittest.mock import MagicMock

import pytest

from ha_bridge import HomeAssistantBridge

jinja2 = pytest.importorskip("jinja2")

E3800_CONTROLS = {
    "ac_charging_power_ios",
    "ups_start_charge_value_as",
    "device_standy_times_as",
    "eco_quite_mode_as",
    "device_touch_locking_as",
}


def _bridge(model="E3800LFP", controls=E3800_CONTROLS, **ha_config):
    device = {
        "device_key": "DK",
        "device_name": model,
        "controls": {c: {} for c in controls},
    }
    config = {"discovery_prefix": "homeassistant", "clear_discovery_on_startup": False}
    config.update(ha_config)
    b = HomeAssistantBridge(config, [device])
    b.client = MagicMock()
    b._connected = True
    return b


def _configs(bridge):
    """topic -> last non-empty discovery payload (parsed)."""
    out = {}
    for call in bridge.client.publish.call_args_list:
        topic, payload = call.args[0], call.args[1]
        if topic.endswith("/config"):
            if payload:
                out[topic] = json.loads(payload)
            else:
                out.pop(topic, None)
    return out


def _render(template, state):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(template).render(value_json=state).strip()


# -------------------- discovery payload validity --------------------


def test_every_switch_and_select_has_a_command_topic():
    for model, controls in (("E3800LFP", E3800_CONTROLS), ("E1500LFP", set())):
        b = _bridge(model, controls)
        b._publish_discovery()
        for topic, config in _configs(b).items():
            component = topic.split("/")[1]
            if component in ("switch", "select"):
                assert "command_topic" in config, topic


def test_bypass_is_a_tsl_gated_binary_sensor():
    b = _bridge()
    b._publish_discovery()
    pubs = {c.args[0]: c.args[1] for c in b.client.publish.call_args_list}
    assert pubs["homeassistant/switch/pecron_DK/bypass/config"] == ""
    assert "homeassistant/binary_sensor/pecron_DK/bypass/config" not in _configs(b)

    b = _bridge(controls=E3800_CONTROLS | {"bypass_enable"})
    b._publish_discovery()
    config = _configs(b)["homeassistant/binary_sensor/pecron_DK/bypass/config"]
    assert config["payload_on"] == "ON" and config["payload_off"] == "OFF"
    assert "homeassistant/switch/pecron_DK/bypass/config" not in _configs(b)


def test_select_templates_render_options_and_skip_missing_fields():
    b = _bridge()
    b._publish_discovery()
    configs = _configs(b)
    threshold = configs["homeassistant/select/pecron_DK/ups_charge_threshold/config"]
    power = configs["homeassistant/select/pecron_DK/ac_charging_power/config"]

    assert "unit_of_measurement" not in threshold
    for value in ("100", 30, "70"):
        rendered = _render(threshold["value_template"], {"ups_start_charge_value_as": value})
        assert rendered in threshold["options"], value
    for value in ("10", 0, "5"):
        rendered = _render(power["value_template"], {"ac_charging_power_ios": value})
        assert rendered in power["options"], value

    # An empty render is ignored by HA's MQTT select; '%' or the raw JSON
    # payload is logged as an invalid option.
    for config in (threshold, power):
        assert _render(config["value_template"], {"soc_percent": 90}) == ""
        assert _render(config["value_template"], {"ups_start_charge_value_as": None}) == ""
        assert _render(config["value_template"], {"ac_charging_power_ios": None}) == ""


# -------------------- state payload --------------------


def _pack(status):
    return {"charging_pack_status": status, "charging_pack_battery": 0}


def test_expansion_pack_state_derived_from_slots_when_device_omits_it():
    b = _bridge("E1500LFP", set())
    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(4)] * 4})
    assert b._state_cache["DK"]["add_bat_status_hm"] == "OFF"

    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(4), _pack(1), _pack(4), _pack(4)]})
    assert b._state_cache["DK"]["add_bat_status_hm"] == "ON"

    # A device that does report the field wins.
    b.publish_state(
        "DK",
        {"add_bat_status_hm": False, "charging_pack_data_jdb": [_pack(1)] + [_pack(4)] * 3},
    )
    assert b._state_cache["DK"]["add_bat_status_hm"] == "OFF"


@pytest.mark.parametrize(
    ("field", "raw", "label"),
    [
        ("machine_screen_light_as", "4", "100%"),
        ("ac_output_voltage_io", 2, "120V"),
        ("ac_output_frequency_io", "1", "60Hz"),
        ("noastime_io", 0, "Off"),
        ("device_standy_times_as", 4, "Always On"),
    ],
)
def test_settings_sensors_publish_labels(field, raw, label):
    b = _bridge()
    b.publish_state("DK", {field: raw})
    assert b._state_cache["DK"][field] == label


def test_select_fields_stay_raw_for_their_templates():
    b = _bridge()
    b.publish_state("DK", {"ac_charging_power_ios": "10", "ups_start_charge_value_as": "100"})
    assert b._state_cache["DK"]["ac_charging_power_ios"] == "10"
    assert b._state_cache["DK"]["ups_start_charge_value_as"] == "100"


def test_retained_state_seeds_cache_so_first_payload_is_complete():
    b = _bridge()
    retained = {"device_status_hm": "Charging", "voltage": 56.7, "temperature": 32}
    msg = MagicMock(topic="pecron/DK/state", payload=json.dumps(retained).encode(), retain=True)
    b._on_state_message(msg)

    b.publish_state("DK", {"host_packet_data_jdb": {"host_packet_voltage": 56.9}})

    published = json.loads(b.client.publish.call_args_list[-1].args[1])
    assert published["device_status_hm"] == "Charging"
    assert published["temperature"] == 32
    assert published["voltage"] == 56.9
    b.client.unsubscribe.assert_called_with("pecron/DK/state")


def test_live_state_echo_does_not_seed_cache():
    b = _bridge()
    b._state_cache["DK"] = {"voltage": 57.0}
    msg = MagicMock(topic="pecron/DK/state", payload=b'{"voltage": 1.0, "x": 1}', retain=False)
    b._on_state_message(msg)
    assert b._state_cache["DK"] == {"voltage": 57.0}


# -------------------- reconnect --------------------


def test_discovery_is_cleared_only_on_the_first_connect():
    b = _bridge(clear_discovery_on_startup=True)
    b._publish_discovery()
    first = [c.args[1] for c in b.client.publish.call_args_list]
    assert "" in first

    b.client.reset_mock()
    b._publish_discovery()
    configs = {
        c.args[0]: c.args[1]
        for c in b.client.publish.call_args_list
        if c.args[0] in b._published_topics
    }
    assert configs and all(payload != "" for payload in configs.values())
    calls_per_topic = {}
    for c in b.client.publish.call_args_list:
        calls_per_topic[c.args[0]] = calls_per_topic.get(c.args[0], 0) + 1
    assert all(calls_per_topic[t] == 1 for t in b._published_topics)


# -------------------- availability --------------------


def test_every_entity_follows_bridge_and_device_availability():
    b = _bridge()
    b._publish_discovery()
    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(1)]})
    configs = _configs(b)
    assert configs
    for topic, config in configs.items():
        assert config["availability"] == [
            {"topic": "pecron/bridge/availability"},
            {"topic": "pecron/DK/availability"},
        ], topic
        assert config["availability_mode"] == "all", topic


def _availability(bridge, topic):
    payloads = [
        (c.args[1], c.kwargs.get("retain"))
        for c in bridge.client.publish.call_args_list
        if c.args[0] == topic
    ]
    return payloads


def test_device_goes_offline_when_no_reading_for_stale_after(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("ha_bridge.time.time", lambda: now[0])
    b = _bridge(stale_after=600)

    b.publish_state("DK", {"voltage": 56.0})
    assert _availability(b, "pecron/DK/availability") == [("online", True)]

    b.client.reset_mock()
    now[0] += 599
    b.check_availability()
    b.publish_state("DK", {"voltage": 56.1})
    assert _availability(b, "pecron/DK/availability") == []

    now[0] += 601
    b.check_availability()
    b.check_availability()
    assert _availability(b, "pecron/DK/availability") == [("offline", True)]

    b.client.reset_mock()
    b.publish_state("DK", {"voltage": 56.2})
    assert _availability(b, "pecron/DK/availability") == [("online", True)]


def test_device_that_never_reports_goes_offline_after_stale_after(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("ha_bridge.time.time", lambda: now[0])
    b = _bridge(stale_after=600)
    b._mark_started()
    now[0] += 601
    b.check_availability()
    assert _availability(b, "pecron/DK/availability") == [("offline", True)]


def test_graceful_disconnect_marks_bridge_offline():
    b = _bridge()
    client = b.client
    b.disconnect()
    assert ("offline", True) in _availability(
        type("B", (), {"client": client})(), "pecron/bridge/availability"
    )
