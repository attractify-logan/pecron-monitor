#!/usr/bin/env python3
"""Tests for HA state_class injection on discovery payloads (issue #78).

Home Assistant only records long-term statistics for numeric sensors that
declare a state_class. Without it, power/voltage/current readings are
display-only and can't feed the Energy Dashboard via a Riemann-sum integral
helper. _pub_config() injects state_class=measurement for instantaneous
measurement sensors, while leaving cumulative energy counters (which set
state_class=total_increasing at their call site) untouched.
"""

import json
import unittest
from unittest.mock import MagicMock

# sys.path + paho mocking are handled globally by tests/conftest.py
from ha_bridge import HomeAssistantBridge, _MEASUREMENT_DEVICE_CLASSES


class TestPubConfigInjectsStateClass(unittest.TestCase):
    def _make_bridge(self):
        b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=[])
        b.client = MagicMock()
        b._published_topics = set()
        return b

    def _last_published_payload(self, bridge):
        args, _ = bridge.client.publish.call_args
        return json.loads(args[1])

    def test_power_sensor_gets_measurement(self):
        b = self._make_bridge()
        b._pub_config(
            "sensor", "DEADBEEF", "ac_output", {"name": "AC Output Power", "device_class": "power"}
        )
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("state_class"), "measurement")

    def test_all_measurement_device_classes_get_tagged(self):
        for dev_class in _MEASUREMENT_DEVICE_CLASSES:
            b = self._make_bridge()
            b._pub_config(
                "sensor", "DEADBEEF", "some_key", {"name": "X", "device_class": dev_class}
            )
            payload = self._last_published_payload(b)
            self.assertEqual(
                payload.get("state_class"),
                "measurement",
                f"device_class {dev_class!r} should get state_class=measurement",
            )

    def test_energy_sensor_is_not_overwritten(self):
        # total_energy sets total_increasing at its call site; we must not clobber it.
        b = self._make_bridge()
        b._pub_config(
            "sensor",
            "DEADBEEF",
            "total_energy",
            {
                "name": "Total PV Energy",
                "device_class": "energy",
                "state_class": "total_increasing",
                "unit_of_measurement": "kWh",
            },
        )
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("state_class"), "total_increasing")

    def test_diagnostic_power_sensor_still_gets_measurement(self):
        # ac_input/dc_input are diagnostic-category but still real measurements;
        # they must get state_class so a Riemann integral can run on them.
        b = self._make_bridge()
        b._pub_config(
            "sensor", "DEADBEEF", "ac_input", {"name": "AC Input Power", "device_class": "power"}
        )
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("entity_category"), "diagnostic")
        self.assertEqual(payload.get("state_class"), "measurement")

    def test_non_measurement_device_class_untouched(self):
        b = self._make_bridge()
        b._pub_config(
            "sensor",
            "DEADBEEF",
            "device_status",
            {"name": "Device Status", "device_class": "enum"},
        )
        payload = self._last_published_payload(b)
        self.assertNotIn("state_class", payload)

    def test_sensor_without_device_class_untouched(self):
        # e.g. AC power factor / frequency entities carry no device_class.
        b = self._make_bridge()
        b._pub_config("sensor", "DEADBEEF", "ac_output_pf", {"name": "AC Power Factor"})
        payload = self._last_published_payload(b)
        self.assertNotIn("state_class", payload)

    def test_switch_component_untouched(self):
        # state_class is a sensor-only concept; switches must never get it.
        b = self._make_bridge()
        b._pub_config("switch", "DEADBEEF", "ac", {"name": "AC Output", "device_class": "outlet"})
        payload = self._last_published_payload(b)
        self.assertNotIn("state_class", payload)

    def test_explicit_state_class_in_payload_is_preserved(self):
        b = self._make_bridge()
        b._pub_config(
            "sensor",
            "DEADBEEF",
            "voltage",
            {"name": "Voltage", "device_class": "voltage", "state_class": "total"},
        )
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("state_class"), "total")

    def test_wb12200_limit_sensors_do_not_get_state_class(self):
        # Regression for #78 review: these config-category sensors carry a numeric
        # device_class (voltage/current) but publish_state decodes them to string
        # labels like "12.8V" / "60A". A numeric state_class on a string state is
        # invalid in HA, so they must be excluded. They are also downgraded from
        # config -> diagnostic (HA rejects config-category sensors).
        cases = [
            ("charging_limit_voltage", "voltage"),
            ("discharge_limit_voltage", "voltage"),
            ("charging_current_limit", "current"),
            ("discharge_current_limit", "current"),
        ]
        for key, dev_class in cases:
            b = self._make_bridge()
            b._pub_config("sensor", "DEADBEEF", key, {"name": key, "device_class": dev_class})
            payload = self._last_published_payload(b)
            self.assertNotIn(
                "state_class",
                payload,
                f"config sensor {key!r} (label state) must not get state_class",
            )
            self.assertEqual(
                payload.get("entity_category"),
                "diagnostic",
                f"{key!r} should be downgraded config -> diagnostic",
            )

    def test_numeric_config_sensor_still_gets_state_class(self):
        # Regression for #78 review (round 2): ac_output_voltage is config-category
        # (downgraded to diagnostic) but it's a genuine numeric voltage reading, so
        # the exclusion must NOT be category-wide -- only the label-backed keys are
        # excluded. ac_output_voltage must keep state_class=measurement.
        b = self._make_bridge()
        b._pub_config(
            "sensor",
            "DEADBEEF",
            "ac_output_voltage",
            {"name": "AC Output Voltage", "device_class": "voltage"},
        )
        payload = self._last_published_payload(b)
        self.assertEqual(payload.get("entity_category"), "diagnostic")
        self.assertEqual(
            payload.get("state_class"),
            "measurement",
            "numeric config sensor ac_output_voltage must keep state_class",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
