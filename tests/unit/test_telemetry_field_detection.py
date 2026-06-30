#!/usr/bin/env python3
"""Tests for PecronMonitor._has_telemetry_fields (issue #84).

E3600/E3800 settings-only local TCP / cloud MQTT payloads include
battery_percentage even though they carry no real telemetry (voltage,
temperature, power). _has_telemetry_fields previously treated bare
battery_percentage as sufficient evidence of "complete telemetry", which
caused the --status collection loop to give up early ("All devices have
telemetry data") while voltage/temperature were still unset, and caused
settings-only local reads to be mislabeled as the primary data source.
"""

from monitor import PecronMonitor


def make_monitor(make_config):
    config = make_config()
    return PecronMonitor(config)


# The exact settings-only payload from issue #84's E3800LFP report --
# battery_percentage plus a pile of settings fields, no voltage/temp/power.
SETTINGS_ONLY_PAYLOAD = {
    "ac_charging_power_ios": "1",
    "ac_output_frequency_io": "1",
    "ac_output_voltage_io": "2",
    "ac_switch_hm": True,
    "auto_light_flag_as": True,
    "battery_percentage": "50",
    "dc_switch_hm": True,
    "device_touch_locking_as": False,
    "eco_quite_mode_as": False,
    "high_frequency_reporting": "0",
    "machine_screen_light_as": "4",
    "noastime_io": "0",
    "ups_start_charge_value_as": "40",
}


def test_settings_only_payload_is_not_telemetry(make_config):
    m = make_monitor(make_config)
    assert m._has_telemetry_fields(SETTINGS_ONLY_PAYLOAD) is False


def test_battery_percentage_with_flat_temp_is_telemetry(make_config):
    """E300LFP/E3800-style flat payloads pair battery_percentage with a flat
    temperature field -- that combination is real telemetry, not settings."""
    m = make_monitor(make_config)
    kv = dict(SETTINGS_ONLY_PAYLOAD, battery_temp=22)
    assert m._has_telemetry_fields(kv) is True


def test_host_packet_data_jdb_with_voltage_is_telemetry(make_config):
    m = make_monitor(make_config)
    kv = {
        "host_packet_data_jdb": {
            "host_packet_voltage": 53.2,
            "host_packet_electric_percentage": 99,
        }
    }
    assert m._has_telemetry_fields(kv) is True


def test_empty_payload_is_not_telemetry(make_config):
    m = make_monitor(make_config)
    assert m._has_telemetry_fields({}) is False
