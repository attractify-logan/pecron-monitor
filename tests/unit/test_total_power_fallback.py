#!/usr/bin/env python3
"""Tests for total_input_power / total_output_power fallback aggregation.

On some models (E3800LFP, E1500LFP) the top-level total_input_power and
total_output_power fields never appear in MQTT packets. The per-source
ac_input_power / dc_input_power (and ac_output_power / dc_output_power)
always do. Fallback computation sums components when the top-level is
absent so HA's Input Power / Output Power entities don't sit at Unknown.
"""

import unittest
from unittest.mock import MagicMock

# sys.path + paho mocking are handled globally by tests/conftest.py
from ha_bridge import HomeAssistantBridge


def make_bridge():
    b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=[])
    b.client = MagicMock()
    b._connected = True
    b._published_topics = set()
    return b


class TestTotalPowerFallback(unittest.TestCase):
    def test_top_level_totals_absent_components_sum(self):
        """Device reports only ac_* and dc_* components, no top-level totals."""
        b = make_bridge()
        # kv shape: top-level ac_data_input_hm + host_packet_data_jdb, no top-level total_*
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 97,
            },
            "ac_data_input_hm": {"ac_input_power": 150},
            "dc_data_input_hm": {"dc_input_power": 20},
            "ac_data_output_hm": {"ac_output_power": 100},
            "dc_data_output_hm": {"dc_output_power": 30},
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        self.assertEqual(
            cache.get("total_input_power"),
            170,
            "total_input_power must fall back to ac+dc=170 when top-level absent",
        )
        self.assertEqual(
            cache.get("total_output_power"),
            130,
            "total_output_power must fall back to ac+dc=130 when top-level absent",
        )

    def test_top_level_totals_present_overrides_fallback(self):
        """If the device DOES report top-level totals, use those; don't overwrite with sum."""
        b = make_bridge()
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 97,
            },
            "total_input_power": 200,
            "total_output_power": 150,
            "ac_data_input_hm": {"ac_input_power": 150},
            "dc_data_input_hm": {"dc_input_power": 20},
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        # host-packet guard suppresses 0 only; 200 is real, must pass through
        self.assertEqual(
            cache.get("total_input_power"),
            200,
            "top-level total_input_power must NOT be overwritten by ac+dc sum",
        )

    def test_zero_components_cache_zero_total(self):
        """When ac/dc components both land as 0 (idle device), fallback publishes 0 so the
        HA Input Power entity shows 0W rather than Unknown."""
        b = make_bridge()
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 97,
            },
            "ac_data_input_hm": {"ac_input_power": 0},
            "dc_data_input_hm": {"dc_input_power": 0},
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        # ac_input_power and dc_input_power both in cache, fallback fires with 0
        self.assertEqual(
            cache.get("total_input_power"),
            0,
            "idle device must publish total_input_power=0, not Unknown",
        )

    def test_no_components_at_all_stays_unknown(self):
        """If neither top-level total nor components arrive, don't invent a 0."""
        b = make_bridge()
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 97,
            },
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        self.assertNotIn("total_input_power", cache)
        self.assertNotIn("total_output_power", cache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
