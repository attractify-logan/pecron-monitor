#!/usr/bin/env python3
"""Tests for TSL-based entity gating (issue #35).

Verifies the _has() helper that decides whether a given device has a specific
TSL resource code, so HA discovery skips entities the device doesn't actually
support instead of emitting them and letting HA show 'Unknown' forever.
"""

import unittest

# sys.path + paho mocking are handled globally by tests/conftest.py
from ha_bridge import HomeAssistantBridge


# Realistic TSL caches pulled from the live devices on Stills.
E1500_TSL = {
    "battery_percentage": {}, "remain_time": {}, "remain_charging_time": {},
    "total_input_power": {}, "total_output_power": {}, "ac_switch_hm": {},
    "dc_switch_hm": {}, "ups_status_hm": {}, "device_status_hm": {},
    "auto_light_flag_as": {}, "add_bat_status_hm": {},
    "ac_data_input_hm": {}, "dc_data_input_hm": {},
    "ac_data_output_hm": {}, "dc_data_output_hm": {},
    "ac_output_voltage_io": {}, "ac_output_frequency_io": {},
    "noastime_io": {}, "machine_screen_light_as": {},
    "charging_pack_data_jdb": {}, "host_packet_data_jdb": {},
    "device_manual": {}, "high_frequency_reporting": {},
}

E3800_TSL = {
    **E1500_TSL,
    # E3800-only additions
    "battery_temp": {}, "charging_plate_temp": {}, "inverter_temp": {},
    "eco_quite_mode_as": {}, "device_touch_locking_as": {},
    "ac_charging_power_ios": {}, "ups_start_charge_value_as": {},
    "device_standy_times_as": {}, "total_energy": {},
    "timing_off_as": {}, "ota_ots": {},
}


class TestHasHelper(unittest.TestCase):

    def test_present_code_returns_true(self):
        device = {"controls": E3800_TSL}
        self.assertTrue(HomeAssistantBridge._has(device, "battery_temp"))

    def test_missing_code_returns_false(self):
        device = {"controls": E1500_TSL}
        self.assertFalse(HomeAssistantBridge._has(device, "battery_temp"))

    def test_empty_controls_returns_false(self):
        device = {"controls": {}}
        self.assertFalse(HomeAssistantBridge._has(device, "battery_temp"))

    def test_missing_controls_key_returns_false(self):
        device = {}
        self.assertFalse(HomeAssistantBridge._has(device, "battery_temp"))

    def test_none_controls_returns_false(self):
        device = {"controls": None}
        self.assertFalse(HomeAssistantBridge._has(device, "battery_temp"))

    def test_or_semantics_across_multiple_codes(self):
        device = {"controls": E1500_TSL}
        self.assertTrue(
            HomeAssistantBridge._has(device, "battery_temp", "battery_percentage"),
            "OR: at least one code present should return True"
        )
        self.assertFalse(
            HomeAssistantBridge._has(device, "battery_temp", "unknown_code"),
            "OR: all missing should return False"
        )


class TestE1500VsE3800Coverage(unittest.TestCase):
    """The 9 TSL codes gated in ha_bridge.py are distinguishable E3800 vs E1500.
    This protects against regressions where someone adds a new entity that
    should be gated but forgets, or accidentally gates on a shared code."""

    E3800_ONLY_CODES = [
        "battery_temp",
        "charging_plate_temp",
        "inverter_temp",
        "eco_quite_mode_as",
        "device_touch_locking_as",
        "ac_charging_power_ios",
        "ups_start_charge_value_as",
        "device_standy_times_as",
        "total_energy",
    ]

    def test_e1500_lacks_all_gated_codes(self):
        for code in self.E3800_ONLY_CODES:
            self.assertNotIn(code, E1500_TSL,
                             f"E1500 should not have {code!r}, if it does the gate rule is wrong")

    def test_e3800_has_all_gated_codes(self):
        for code in self.E3800_ONLY_CODES:
            self.assertIn(code, E3800_TSL,
                          f"E3800 must have {code!r} (otherwise gated entity never publishes)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
