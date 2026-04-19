#!/usr/bin/env python3
"""Tests for HA entity_category hint routing (issue #34).

Verifies that non-essential entities get bucketed under HA's Configuration /
Diagnostic sections via entity_category, while primary entities stay in the
main device view.
"""

import json
import unittest
from unittest.mock import MagicMock

# sys.path + paho mocking are handled globally by tests/conftest.py
from ha_bridge import HomeAssistantBridge, ENTITY_CATEGORIES, entity_category_for


class TestEntityCategoryHelper(unittest.TestCase):
    """entity_category_for() routing: main view for daily-glance entities,
    config for rarely-touched knobs, diagnostic for detail readouts."""

    def test_primary_entities_default_to_main_view(self):
        for key in ["battery", "voltage", "temperature", "ac", "dc",
                    "remaining_time", "total_input", "total_output",
                    "current", "total_energy"]:
            self.assertIsNone(entity_category_for(key),
                              f"{key!r} should stay in main view (no category)")

    def test_config_entities(self):
        for key in ["ups", "eco_mode", "touch_lock", "bypass", "auto_dim",
                    "screen_brightness", "ac_output_voltage", "ac_output_hz",
                    "charging_limit_voltage", "battery_heating", "beep"]:
            self.assertEqual(entity_category_for(key), "config",
                             f"{key!r} should be in Configuration section")

    def test_diagnostic_entities(self):
        for key in ["host_battery", "battery_temp", "charging_plate_temp",
                    "inverter_temp", "ac_input", "dc_input",
                    "remaining_charging_time", "fault_alarm", "device_status"]:
            self.assertEqual(entity_category_for(key), "diagnostic",
                             f"{key!r} should be in Diagnostic section")

    def test_pack_sensors_are_diagnostic_by_prefix(self):
        for n in range(4):
            for suffix in ["battery", "voltage", "current", "temp", "status"]:
                key = f"pack_{n}_{suffix}"
                self.assertEqual(entity_category_for(key), "diagnostic",
                                 f"{key!r} should be Diagnostic via pack_ prefix")

    def test_unknown_key_stays_in_main_view(self):
        # Future new entity types should default to main view without opting in.
        self.assertIsNone(entity_category_for("some_future_entity"))
        self.assertIsNone(entity_category_for(""))

    def test_solar_ports_are_main_view(self):
        """GX16 solar inputs live in main view because van/off-grid setups
        care about them at a glance. DC5521 (usually the AC-adapter jack)
        stays diagnostic."""
        for key in ["gx16mf1_input_voltage", "gx16mf1_input_current", "gx16mf1_input_power",
                    "gx16mf2_input_voltage", "gx16mf2_input_current", "gx16mf2_input_power"]:
            self.assertIsNone(entity_category_for(key),
                              f"solar port {key} must be main view, not diagnostic")
        for key in ["dc5521_input_voltage", "dc5521_input_current", "dc5521_input_power"]:
            self.assertEqual(entity_category_for(key), "diagnostic",
                             f"DC5521 barrel jack {key} stays diagnostic")


class TestPubConfigInjectsCategory(unittest.TestCase):
    """_pub_config() should inject entity_category into the discovery payload
    for categorized keys, leaving primary keys untouched."""

    def _make_bridge(self):
        b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=[])
        b.client = MagicMock()
        b._published_topics = set()
        return b

    def _last_published_payload(self, bridge):
        # client.publish(topic, json_str, qos=1, retain=True) captured via mock
        args, kwargs = bridge.client.publish.call_args
        return json.loads(args[1])

    def test_primary_entity_not_tagged(self):
        b = self._make_bridge()
        b._pub_config("sensor", "DEADBEEF", "battery", {"name": "Battery"})
        payload = self._last_published_payload(b)
        self.assertNotIn("entity_category", payload,
                         "Primary entities must not get a category")

    def test_config_entity_tagged(self):
        b = self._make_bridge()
        b._pub_config("switch", "DEADBEEF", "ups", {"name": "UPS"})
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("entity_category"), "config")

    def test_diagnostic_entity_tagged(self):
        b = self._make_bridge()
        b._pub_config("sensor", "DEADBEEF", "battery_temp", {"name": "Batt Temp"})
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("entity_category"), "diagnostic")

    def test_pack_entity_tagged_diagnostic(self):
        b = self._make_bridge()
        b._pub_config("sensor", "DEADBEEF", "pack_2_voltage", {"name": "Pack 2 V"})
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("entity_category"), "diagnostic")

    def test_explicit_category_in_payload_is_preserved(self):
        # If a call site already set entity_category explicitly, don't overwrite.
        b = self._make_bridge()
        b._pub_config("sensor", "DEADBEEF", "battery_temp",
                      {"name": "Batt Temp", "entity_category": "config"})
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("entity_category"), "config",
                         "Explicit category in call-site payload must win")

    def test_publish_call_shape_unchanged(self):
        """Ensure we didn't accidentally break qos/retain/topic construction."""
        b = self._make_bridge()
        b._pub_config("sensor", "DEADBEEF", "battery", {"name": "Battery"})
        args, kwargs = b.client.publish.call_args
        self.assertEqual(args[0], "homeassistant/sensor/pecron_DEADBEEF/battery/config")
        self.assertEqual(kwargs.get("qos"), 1)
        self.assertEqual(kwargs.get("retain"), True)


class TestCategoryCoverage(unittest.TestCase):
    """Sanity: every key in ENTITY_CATEGORIES maps to one of the two HA-supported
    category values. Typos in the map (e.g. 'configuration' vs 'config') would
    cause HA to ignore the field silently."""

    def test_only_valid_category_values(self):
        for key, cat in ENTITY_CATEGORIES.items():
            self.assertIn(cat, ("config", "diagnostic"),
                          f"{key!r}: {cat!r} is not a valid HA entity_category")


if __name__ == "__main__":
    unittest.main(verbosity=2)
