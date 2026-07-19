"""Characterization tests for Home Assistant state publication."""

import json
from unittest.mock import MagicMock, call, patch

from ha_bridge import HomeAssistantBridge


def _device():
    return {"device_key": "DEV1", "device_name": "E3800LFP", "controls": {}}


def _bridge(*, energy_sensors=False):
    bridge = HomeAssistantBridge(
        {
            "discovery_prefix": "homeassistant",
            "clear_discovery_on_startup": False,
            "energy_sensors": energy_sensors,
        },
        [_device()],
    )
    bridge.client = MagicMock()
    bridge._connected = True
    return bridge


def _full_telemetry():
    return {
        "host_packet_data_jdb": {
            "host_packet_electric_percentage": "83",
            "host_packet_voltage": "51.26",
            "host_packet_temp": "32.9",
            "host_packet_current": "-12.345",
        },
        "battery_percentage": 91,
        "battery_temp": "41.8",
        "charging_plate_temp": 42.7,
        "inverter_temp": "43.6",
        "total_input_power": "1500.9",
        "total_output_power": 765.8,
        "total_energy": "12.34567",
        "ac_data_input_hm": {"ac_input_power": "1400"},
        "dc_data_input_hm": {
            "dc_input_power": 100.9,
            "dc5521_input_voltage": 0,
            "dc5521_input_current": "0.00",
            "dc5521_input_power": 0,
            "gx16mf1_input_voltage": "42.24",
            "gx16mf1_input_current": "9.876",
            "gx16mf1_input_power": "417.9",
            "gx16mf2_input_voltage": 0,
            "gx16mf2_input_current": 0,
            "gx16mf2_input_power": 0,
        },
        "remain_time": "1565",
        "remain_charging_time": 125.9,
        "ac_switch_hm": "1",
        "dc_switch_hm": 0,
        "ups_status_hm": "true",
        "add_bat_status_hm": "off",
        "eco_quite_mode_as": "enabled",
        "device_touch_locking_as": 1,
        "bypass_enable": 0,
        "auto_light_flag_as": "on",
        "ac_charging_power_ios": "600",
        "ups_start_charge_value_as": 20,
        "device_standy_times_as": 3,
        "machine_screen_light_as": 75,
        "ac_output_voltage_io": 230,
        "ac_output_frequency_io": 60,
        "noastime_io": 15,
        "charging_limit_voltage": 5,
        "discharge_limiting_voltage": "4",
        "charging_current_limit": 7,
        "discharge_limiting_current": 6,
        "battery_heating_mode": 1,
        "FAULT_ALARM_ENUM": 13,
        "beep_voice_us": "false",
        "battery_indicator_us": "yes",
        "ac_data_output_hm": {
            "ac_output_power": "700.9",
            "ac_output_voltage": "229.8",
            "ac_output_hz": "59.94",
            "ac_output_pf": "0.956",
        },
        "dc_data_output_hm": {"dc_output_power": "65.7"},
        "device_status_hm": 4,
        "charging_pack_data_jdb": [
            {
                "charging_pack_status": 3,
                "charging_pack_battery": "76.9",
                "charging_pack_voltage": "51.26",
                "charging_pack_current": "-4.567",
                "charging_pack_temp": "30.9",
            },
            {
                "charging_pack_status": 4,
                "charging_pack_battery": 99,
                "charging_pack_voltage": 99,
                "charging_pack_current": 99,
                "charging_pack_temp": 99,
            },
            {
                "charging_pack_status": 88,
                "charging_pack_battery": 0,
                "charging_pack_voltage": 50,
                "charging_pack_current": 0,
                "charging_pack_temp": 31,
            },
            "invalid-pack",
        ],
    }


def _expected_full_state():
    return {
        "voltage": 51.3,
        "temperature": 32,
        "battery_temp": 41,
        "charging_plate_temp": 42,
        "inverter_temp": 43,
        "total_input_power": 1500,
        "total_output_power": 765,
        "total_energy": 12.346,
        "ac_input_power": 1400,
        "dc_input_power": 100,
        "remain_minutes": 1565,
        "remain_charging_minutes": 125,
        "remain_hm": "1d02h05m",
        "remain_charging_hm": "2h05m",
        "ac_switch": "ON",
        "dc_switch": "OFF",
        "ups_mode": "ON",
        "add_bat_status_hm": "OFF",
        "eco_quite_mode_as": "ON",
        "device_touch_locking_as": "ON",
        "bypass_enable": "OFF",
        "auto_light_flag_as": "ON",
        "ac_charging_power_ios": "600",
        "ups_start_charge_value_as": 20,
        "device_standy_times_as": 3,
        "machine_screen_light_as": 75,
        "ac_output_voltage_io": 230,
        "ac_output_frequency_io": 60,
        "noastime_io": 15,
        "charging_limit_voltage": "14.6V",
        "discharge_limiting_voltage": "12.0V",
        "charging_current_limit": "120A",
        "discharge_limiting_current": "200A",
        "battery_heating_mode": "Keep Warm",
        "FAULT_ALARM_ENUM": "Charger Overheating",
        "beep_voice_us": "OFF",
        "battery_indicator_us": "ON",
        "ac_output_power": 700,
        "ac_output_voltage": 229,
        "dc_output_power": 65,
        "device_status_hm": "Standby",
        "current": -12.35,
        "dc5521_input_voltage": 0.0,
        "dc5521_input_current": 0.0,
        "dc5521_input_power": 0,
        "gx16mf1_input_voltage": 42.2,
        "gx16mf1_input_current": 9.88,
        "gx16mf1_input_power": 417,
        "gx16mf2_input_voltage": 0.0,
        "gx16mf2_input_current": 0.0,
        "gx16mf2_input_power": 0,
        "ac_output_hz": 59.9,
        "ac_output_pf": 0.96,
        "pack_0_status": "Balanced Charging",
        "pack_0_battery": 76,
        "pack_0_voltage": 51.3,
        "pack_0_current": -4.57,
        "pack_0_temp": 30,
        "pack_1_status": None,
        "pack_1_battery": None,
        "pack_1_voltage": None,
        "pack_1_current": None,
        "pack_1_temp": None,
        "pack_2_status": "88",
        "pack_2_battery": 88,
        "pack_2_voltage": 50.0,
        "pack_2_current": 0.0,
        "pack_2_temp": 31,
        "host_percent": 83,
        "soc_percent": 83,
    }


def test_publish_state_emits_exact_comprehensive_payload_and_defers_port_discovery_once():
    bridge = _bridge()
    bridge._published_topics = set()
    bridge._device_dev_info["DEV1"] = {
        "identifiers": ["pecron_DEV1"],
        "name": "E3800LFP",
        "manufacturer": "Pecron",
        "model": "E3800LFP",
    }

    bridge.publish_state("DEV1", _full_telemetry())

    expected_state = _expected_full_state()
    state_call = call("pecron/DEV1/state", json.dumps(expected_state), qos=1, retain=True)
    assert bridge.client.publish.call_args_list[-1] == state_call

    config_calls = [
        published
        for published in bridge.client.publish.call_args_list
        if published.args[0].endswith("/config")
    ]
    assert [published.args[0] for published in config_calls] == [
        f"homeassistant/sensor/pecron_DEV1/{port}_input_{measurement}/config"
        for port in ("dc5521", "gx16mf1", "gx16mf2")
        for measurement in ("voltage", "current", "power")
    ]
    assert all(published.kwargs == {"qos": 1, "retain": True} for published in config_calls)
    assert bridge._deferred_ports_published == {
        ("DEV1", "dc5521"),
        ("DEV1", "gx16mf1"),
        ("DEV1", "gx16mf2"),
    }

    bridge.client.reset_mock()
    partial = {
        "host_packet_data_jdb": {
            "host_packet_electric_percentage": 82,
            "host_packet_voltage": 0,
            "host_packet_temp": None,
            "host_packet_current": 0,
        },
        "total_input_power": 0,
        "total_output_power": 0,
        "ac_data_input_hm": {"ac_input_power": 0},
        "dc_data_input_hm": {
            "dc_input_power": None,
            "dc5521_input_voltage": 0,
            "dc5521_input_current": 0,
            "dc5521_input_power": 0,
        },
        "remain_time": 0,
        "ac_switch_hm": 0,
        "dc_switch_hm": None,
        "eco_quite_mode_as": None,
        "device_touch_locking_as": 0,
        "ac_charging_power_ios": None,
        "noastime_io": 0,
        "FAULT_ALARM_ENUM": None,
        "battery_indicator_us": None,
        "ac_data_output_hm": {
            "ac_output_power": 0,
            "ac_output_voltage": 0,
        },
        "dc_data_output_hm": {"dc_output_power": 0},
    }
    bridge.publish_state("DEV1", partial)

    expected_state.update(
        {
            "ac_input_power": 0,
            "ac_switch": "OFF",
            "device_touch_locking_as": "OFF",
            "noastime_io": 0,
            "ac_output_power": 0,
            "ac_output_voltage": 0,
            "dc_output_power": 0,
            "current": 0.0,
            "host_percent": 82,
        }
    )
    assert bridge.client.publish.call_args_list == [
        call("pecron/DEV1/state", json.dumps(expected_state), qos=1, retain=True)
    ]


def test_publish_state_does_not_claim_availability_before_mqtt_connects():
    bridge = _bridge()
    bridge._connected = False

    bridge.publish_state("DEV1", _full_telemetry())

    bridge.client.publish.assert_not_called()
    assert bridge._state_cache == {}


def test_enabled_energy_integrator_receives_only_observed_power_sources():
    integrator = MagicMock()
    integrator.update.return_value = {
        "ac_input": 0.1234567894,
        "ac_output": 1,
    }
    with patch("ha_bridge.EnergyIntegrator", return_value=integrator) as constructor:
        bridge = _bridge(energy_sensors="yes")

    constructor.assert_called_once_with(configured_path=None, max_gap_seconds=1800.0)
    payload = {
        "ac_data_input_hm": {"ac_input_power": 100},
        "ac_data_output_hm": {"ac_output_power": None},
    }
    bridge.publish_state("DEV1", payload)

    integrator.update.assert_called_once_with("DEV1", {"ac_input": 100, "ac_output": None})
    expected = {
        "ac_input_power": 100,
        "remain_hm": None,
        "remain_charging_hm": None,
        "host_percent": None,
        "soc_percent": None,
        "ac_input_energy": 0.123456789,
        "ac_output_energy": 1,
    }
    bridge.client.publish.assert_called_once_with(
        "pecron/DEV1/state", json.dumps(expected), qos=1, retain=True
    )
