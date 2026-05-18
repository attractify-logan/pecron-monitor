"""Tests for HA discovery clear-and-republish behavior."""

import json
from unittest.mock import MagicMock

from ha_bridge import HomeAssistantBridge


def _device(device_key="DEV1"):
    return {
        "device_key": device_key,
        "device_name": "E1500LFP",
        "controls": {
            "battery_percentage": {},
            "voltage": {},
            "total_input_power": {},
            "total_output_power": {},
            "ac_switch_hm": {},
            "dc_switch_hm": {},
            "ups_status_hm": {},
        },
    }


def _bridge(clear=True):
    bridge = HomeAssistantBridge(
        {"discovery_prefix": "homeassistant", "clear_discovery_on_startup": clear},
        devices=[_device()],
    )
    bridge.client = MagicMock()
    bridge._connected = True
    return bridge


def test_publish_discovery_clears_current_topic_before_republish():
    bridge = _bridge(clear=True)

    bridge._publish_discovery()

    topic = "homeassistant/sensor/pecron_DEV1/battery/config"
    calls = [call.args for call in bridge.client.publish.call_args_list if call.args[0] == topic]
    assert len(calls) >= 2
    assert calls[0] == (topic, "")
    payload = json.loads(calls[1][1])
    assert payload["unique_id"] == "pecron_DEV1_battery"


def test_republished_config_topics_remain_tracked_not_stale_cleared():
    bridge = _bridge(clear=True)

    bridge._publish_discovery()

    topic = "homeassistant/sensor/pecron_DEV1/battery/config"
    assert topic in bridge._published_topics
    # Exactly two publishes to a current topic: explicit clear, then new config.
    # _clear_stale_entities must not clear it a third time as stale.
    calls = [call.args for call in bridge.client.publish.call_args_list if call.args[0] == topic]
    assert len(calls) == 2


def test_clear_discovery_can_be_disabled_for_legacy_behavior():
    bridge = _bridge(clear=False)

    bridge._publish_discovery()

    topic = "homeassistant/sensor/pecron_DEV1/battery/config"
    calls = [call.args for call in bridge.client.publish.call_args_list if call.args[0] == topic]
    assert len(calls) == 1
    assert calls[0][1] != ""
