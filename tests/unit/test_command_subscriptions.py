#!/usr/bin/env python3
"""Tests for issue #49: every switch published to HA discovery with a
command_topic must also have its command_topic subscribed. Previously the
subscribe step hardcoded ["ac", "dc", "ups"] and silently dropped commands
sent to eco_mode, touch_lock, auto_light_flag_as, etc.

The fix drives the subscribe loop from a list (`_command_topics`) populated
during `_publish_discovery`, so adding a new switch to discovery
automatically wires its subscription.
"""

import unittest
from unittest.mock import MagicMock

# sys.path + paho mocking are handled globally by tests/conftest.py
from ha_bridge import HomeAssistantBridge


def make_bridge(devices):
    """Build a bridge with a mocked MQTT client.

    Mirrors the pattern from test_ghost_suppression.make_bridge but accepts a
    custom devices list since these tests need a device with TSL controls
    populated to exercise the gated switches (eco_mode, touch_lock).
    """
    b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=devices)
    b.client = MagicMock()
    b._connected = True
    return b


def _e3800_device(dk="DEV1"):
    """An E3800-shaped device with the TSL controls that gate the eco_mode and
    touch_lock switches. is_pps is derived from the name (no 'WB' substring),
    so device_name picks up the PPS branch in _publish_discovery.
    """
    return {
        "device_key": dk,
        "device_name": "E3800",
        "controls": {
            # Required for eco_mode switch
            "eco_quite_mode_as": {},
            # Required for touch_lock switch
            "device_touch_locking_as": {},
            # Misc TSL codes that gate other entities; harmless to include
            "battery_temp": {},
            "charging_plate_temp": {},
        },
    }


class TestCommandTopicSubscriptions(unittest.TestCase):
    """Issue #49: every command_topic registered in discovery must be
    subscribed to. Otherwise HA's toggle UI looks live but the command
    publishes are silently dropped because the bridge never receives them.
    """

    def test_publish_discovery_captures_all_switch_command_topics(self):
        """After _publish_discovery runs, _command_topics must include the
        command_topic of every switch entity registered, not just ac/dc/ups."""
        b = make_bridge([_e3800_device("BC2A33E2B4BB")])
        b._publish_discovery()
        topics = set(b._command_topics)

        # Core switches (always published)
        self.assertIn("pecron/BC2A33E2B4BB/ac/set", topics)
        self.assertIn("pecron/BC2A33E2B4BB/dc/set", topics)
        self.assertIn("pecron/BC2A33E2B4BB/ups/set", topics)

        # E3800-specific switches that previously had discovery without
        # subscription. Without these, HA toggles do nothing.
        self.assertIn("pecron/BC2A33E2B4BB/eco_mode/set", topics,
                      "eco_mode command_topic must be subscribed (issue #49)")
        self.assertIn("pecron/BC2A33E2B4BB/touch_lock/set", topics,
                      "touch_lock command_topic must be subscribed (issue #49)")
        self.assertIn("pecron/BC2A33E2B4BB/auto_light_flag_as/set", topics,
                      "auto_light_flag_as (auto_dim) command_topic must be subscribed (issue #49)")

    def test_command_topics_have_no_duplicates(self):
        """Discovery is sometimes re-run after reconnect; the captured list
        must not double-subscribe topics."""
        b = make_bridge([_e3800_device("DEV1")])
        b._publish_discovery()
        first = list(b._command_topics)
        # Re-run discovery; _command_topics is reset at the top of
        # _publish_discovery so the second run produces the same set.
        b._publish_discovery()
        second = list(b._command_topics)
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual(len(set(second)), len(second),
                         "no duplicates expected within a single discovery pass")

    def test_command_topics_capture_per_device(self):
        """With multiple devices, every device's command_topics must show up.
        Regression guard for any future per-device gating bug."""
        b = make_bridge([_e3800_device("DEV1"), _e3800_device("DEV2")])
        b._publish_discovery()
        topics = set(b._command_topics)
        for dk in ["DEV1", "DEV2"]:
            for ctrl in ["ac", "dc", "ups", "eco_mode", "touch_lock", "auto_light_flag_as"]:
                self.assertIn(f"pecron/{dk}/{ctrl}/set", topics,
                              f"missing {ctrl} for {dk}")

    def test_subscribe_called_for_every_command_topic_on_connect(self):
        """End-to-end: simulate the on_connect path by invoking
        _publish_discovery + the subscribe loop the way the real callback
        does, and assert client.subscribe was called for every switch
        command_topic.

        We invoke the subscribe loop directly (rather than the full
        _connect_attempt / on_connect path) because on_connect is a closure
        around the local `client` variable; the public surface tested here is
        the contract: _command_topics drives subscription.
        """
        b = make_bridge([_e3800_device("BC2A33E2B4BB")])
        b._publish_discovery()
        # Mirror the on_connect subscribe loop:
        for topic in b._command_topics:
            b.client.subscribe(topic, qos=1)

        subscribed = {call.args[0] for call in b.client.subscribe.call_args_list}
        for ctrl in ["ac", "dc", "ups", "eco_mode", "touch_lock", "auto_light_flag_as"]:
            self.assertIn(f"pecron/BC2A33E2B4BB/{ctrl}/set", subscribed,
                          f"client.subscribe never called for {ctrl}/set")
        # Sanity: every subscribe call uses qos=1 (matches the production loop)
        for call in b.client.subscribe.call_args_list:
            self.assertEqual(call.kwargs.get("qos"), 1)

    def test_eco_mode_skipped_when_tsl_lacks_control(self):
        """Devices without eco_quite_mode_as in their TSL must not register
        eco_mode discovery, AND must not subscribe to a phantom eco_mode/set
        topic. Same pattern for touch_lock. This guards against the inverse
        of issue #49: subscribing to a topic HA never advertises.
        """
        device = {
            "device_key": "E1500A",
            "device_name": "E1500LFP",
            "controls": {},  # No E3800-specific TSL codes
        }
        b = make_bridge([device])
        b._publish_discovery()
        topics = set(b._command_topics)
        # Core switches still subscribed
        self.assertIn("pecron/E1500A/ac/set", topics)
        self.assertIn("pecron/E1500A/dc/set", topics)
        self.assertIn("pecron/E1500A/ups/set", topics)
        # E3800-only switches NOT subscribed (TSL gate skipped them)
        self.assertNotIn("pecron/E1500A/eco_mode/set", topics)
        self.assertNotIn("pecron/E1500A/touch_lock/set", topics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
