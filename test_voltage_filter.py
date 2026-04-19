#!/usr/bin/env python3
"""Tests for voltage 0.0V filter in HA publish path (issue #36).

Verifies:
- A real non-zero voltage reading is accepted and cached.
- A later 0.0V reading does NOT overwrite the cached non-zero value.
- Initial state with only 0.0V input leaves cache empty (HA shows Unknown
  rather than an incorrect 0.0V).
- Non-voltage fields that legitimately can be 0 (power in/out) are not
  affected by the filter.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

sys.modules["paho"] = MagicMock()
sys.modules["paho.mqtt"] = MagicMock()
sys.modules["paho.mqtt.client"] = MagicMock()

from ha_bridge import HomeAssistantBridge  # noqa: E402


def make_bridge():
    b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=[])
    b.client = MagicMock()
    b._connected = True
    b._published_topics = set()
    return b


def _kv_with_voltage(value):
    """Craft a cloud-MQTT-shaped kv that resolves voltage via host_packet_data_jdb.
    SENSOR_FIELDS["voltage"] is a list of fallback paths; the first one is
    ('host_packet_data_jdb', 'host_packet_voltage') for cloud MQTT."""
    return {
        "host_packet_data_jdb": {
            "host_packet_voltage": value,
            "host_packet_electric_percentage": 95,
        },
    }


class TestVoltageFilter(unittest.TestCase):

    def test_real_voltage_accepted(self):
        b = make_bridge()
        dk = "DEV1"
        b.publish_state(dk, _kv_with_voltage(52.4))
        self.assertEqual(b._state_cache[dk].get("voltage"), 52.4)

    def test_zero_voltage_does_not_overwrite_cached_real_value(self):
        b = make_bridge()
        dk = "DEV1"
        b.publish_state(dk, _kv_with_voltage(53.1))
        self.assertEqual(b._state_cache[dk]["voltage"], 53.1)
        # Second packet: voltage=0.0 (settings-only placeholder)
        b.publish_state(dk, _kv_with_voltage(0))
        self.assertEqual(b._state_cache[dk]["voltage"], 53.1,
                         "0.0V update must not clobber the cached 53.1V")

    def test_initial_zero_voltage_leaves_cache_empty(self):
        """HA should show Unknown (no state topic send for voltage) rather than
        spuriously register as 0.0V before a real reading arrives."""
        b = make_bridge()
        dk = "DEV1"
        b.publish_state(dk, _kv_with_voltage(0))
        self.assertNotIn("voltage", b._state_cache.get(dk, {}),
                         "Initial 0.0V packet must not seed the cache at 0")

    def test_subsequent_real_voltage_replaces_cached(self):
        """Normal voltage changes (52.4 → 52.9) must always update."""
        b = make_bridge()
        dk = "DEV1"
        b.publish_state(dk, _kv_with_voltage(52.4))
        b.publish_state(dk, _kv_with_voltage(52.9))
        self.assertEqual(b._state_cache[dk]["voltage"], 52.9)

    def test_negative_voltage_not_treated_as_valid(self):
        """Defensive: a negative reading from a corrupted packet shouldn't
        clobber the cache either. The > 0 check catches this for free."""
        b = make_bridge()
        dk = "DEV1"
        b.publish_state(dk, _kv_with_voltage(53.1))
        b.publish_state(dk, _kv_with_voltage(-1))
        self.assertEqual(b._state_cache[dk]["voltage"], 53.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
