from unittest.mock import MagicMock, call, patch

from monitor import PecronMonitor


def _device(device_key, product_name, device_name):
    return {
        "product_key": "synthetic",
        "device_key": device_key,
        "product_name": product_name,
        "device_name": device_name,
        "controls": {},
    }


def _make_monitor(devices, latest_data, data_sources=None, offline=True):
    monitor = PecronMonitor({"region": "na"})
    monitor.devices = devices
    monitor.latest_data = latest_data
    monitor.data_sources = data_sources or {}
    monitor.offline_mode = offline
    monitor.authenticate = MagicMock()
    monitor.connect_mqtt = MagicMock()
    monitor._request_status = MagicMock()
    monitor._enable_high_freq_reporting = MagicMock()
    monitor._disable_high_freq_reporting = MagicMock()
    return monitor


def _run_status(monitor, capsys, force_offline=True):
    with patch("monitor_status.time.sleep") as sleep:
        monitor.status_once(force_offline=force_offline)
    return capsys.readouterr().out, sleep


def test_status_once_exact_output_for_e1500_and_e3800(capsys):
    e1500_key = "E1500-SYNTH"
    e3800_key = "E3800-SYNTH"
    monitor = _make_monitor(
        devices=[
            _device(e1500_key, "Mystery Alias", "E-1500 LFP"),
            _device(e3800_key, "E3800 LFP", "Living Room Battery"),
        ],
        latest_data={
            e1500_key: {
                "host_packet_data_jdb": {
                    "host_packet_electric_percentage": 75,
                    "host_packet_voltage": 48,
                    "host_packet_current": -10,
                    "host_packet_temp": 22,
                },
                "device_status_hm": 3,
                "remain_time": 5,
                "remain_charging_time": 0,
                "total_input_power": 900,
                "total_output_power": 700,
                "ac_data_input_hm": {"ac_input_power": 10},
                "dc_data_input_hm": {"dc_input_power": 20},
                "ac_data_output_hm": {"ac_output_power": 30, "ac_output_voltage": 120},
                "dc_data_output_hm": {"dc_output_power": 40},
                "ac_switch_hm": 1,
                "dc_switch_hm": 0,
                "ups_status_hm": 1,
                "charging_pack_data_jdb": [
                    {
                        "charging_pack_status": 1,
                        "charging_pack_battery": 72,
                        "charging_pack_voltage": 51.2,
                    },
                    {
                        "charging_pack_status": 87,
                        "charging_pack_battery": 0,
                        "charging_pack_voltage": 50,
                    },
                    {
                        "charging_pack_status": 4,
                        "charging_pack_battery": 100,
                        "charging_pack_voltage": 53,
                    },
                ],
            },
            e3800_key: {
                "battery_percentage": 25,
                "battery_temp": 30,
                "host_packet_data_jdb": {
                    "host_packet_voltage": 50,
                    "host_packet_current": 5,
                },
                "remain_time": 125,
                "remain_charging_time": 61,
                "total_input_power": 0,
                "total_output_power": 0,
                "ac_data_input_hm": {"ac_input_power": 40},
                "dc_data_input_hm": {"dc_input_power": 60},
                "ac_data_output_hm": {"ac_output_power": 200, "ac_output_voltage": 120},
                "dc_data_output_hm": {"dc_output_power": 25},
                "ac_switch_hm": 0,
                "dc_switch_hm": 1,
                "ups_status_hm": 0,
            },
        },
        data_sources={e1500_key: "BLE", e3800_key: "CLOUD MQTT"},
    )

    output, sleep = _run_status(monitor, capsys)

    assert output == (
        "\n==================================================\n"
        "Device: E1500-SYNTH\n"
        "Connection: BLE\n"
        "==================================================\n"
        "Status:        AC Discharge (3)\n"
        "Battery:       75%\n"
        "Voltage:       48.0V\n"
        "Temperature:   22°C\n"
        "Discharge time:N/A (unreliable from local)\n"
        "Charge time:   N/A\n"
        "Net Drain:     480.0W\n"
        "Capacity:      1536Wh\n"
        "Est. Empty:    2h 24m\n"
        "Total Input:   900W\n"
        "Total Output:  700W\n"
        "AC Output:     30W @ 120V\n"
        "DC Output:     40W\n"
        "AC Input:      10W\n"
        "DC Input:      20W\n"
        "AC Switch:     ON\n"
        "DC Switch:     OFF\n"
        "UPS Mode:      ON\n"
        "Pack 0:        72% 51.2V\n"
        "Pack 1:        87% 50.0V\n"
        "\n==================================================\n"
        "Device: E3800-SYNTH\n"
        "Connection: CLOUD MQTT\n"
        "==================================================\n"
        "Status:        Unknown (-1)\n"
        "Battery:       25%\n"
        "Voltage:       50.0V\n"
        "Temperature:   30°C\n"
        "Discharge time:2h 5m\n"
        "Charge time:   1h 1m\n"
        "Net Charge:    250.0W\n"
        "Capacity:      3840Wh\n"
        "Est. Full:     11h 31m\n"
        "Total Input:   100W\n"
        "Total Output:  225W\n"
        "AC Output:     200W @ 120V\n"
        "DC Output:     25W\n"
        "AC Input:      40W\n"
        "DC Input:      60W\n"
        "AC Switch:     OFF\n"
        "DC Switch:     ON\n"
        "UPS Mode:      OFF\n"
    )
    monitor.authenticate.assert_called_once_with(force_offline=True, skip_local=None)
    monitor._request_status.assert_called_once_with()
    assert sleep.call_args_list == [call(5)]


def test_status_once_zero_power_has_no_estimate_and_unknown_source(capsys):
    device_key = "E1500-ZERO"
    monitor = _make_monitor(
        devices=[_device(device_key, "E1500LFP", "unused")],
        latest_data={
            device_key: {
                "host_packet_data_jdb": {
                    "host_packet_electric_percentage": 50,
                    "host_packet_voltage": 51.2,
                    "host_packet_current": 0,
                    "host_packet_temp": 20,
                },
                "device_status_hm": 0,
            }
        },
    )

    output, _ = _run_status(monitor, capsys)

    assert output == (
        "\n==================================================\n"
        "Device: E1500-ZERO\n"
        "Connection: UNKNOWN\n"
        "==================================================\n"
        "Status:        Shut Down (0)\n"
        "Battery:       50%\n"
        "Voltage:       51.2V\n"
        "Temperature:   20°C\n"
        "Discharge time:N/A\n"
        "Charge time:   N/A\n"
        "Net Charge:    0.0W\n"
        "Capacity:      1536Wh\n"
        "Total Input:   0W\n"
        "Total Output:  0W\n"
        "AC Output:     0W @ ?V\n"
        "DC Output:     0W\n"
        "AC Input:      0W\n"
        "DC Input:      0W\n"
        "AC Switch:     OFF\n"
        "DC Switch:     OFF\n"
        "UPS Mode:      OFF\n"
    )


def test_status_once_exact_no_data_message(capsys):
    device_key = "OFFLINE-SYNTH"
    monitor = _make_monitor(
        devices=[_device(device_key, "E1500LFP", "Offline")],
        latest_data={},
    )

    output, sleep = _run_status(monitor, capsys)

    assert output == "No data received — device may be offline.\n"
    assert sleep.call_count == 9
    assert monitor._request_status.call_count == 5


def test_force_offline_skips_high_frequency_writes(capsys):
    device_key = "OFFLINE-E1500"
    monitor = _make_monitor(
        devices=[_device(device_key, "E1500LFP", "Offline")],
        latest_data={
            device_key: {
                "host_packet_data_jdb": {
                    "host_packet_electric_percentage": 50,
                    "host_packet_voltage": 50,
                    "host_packet_current": 0,
                    "host_packet_temp": 20,
                }
            }
        },
        offline=True,
    )

    _run_status(monitor, capsys, force_offline=True)

    monitor.connect_mqtt.assert_not_called()
    monitor._enable_high_freq_reporting.assert_not_called()
    monitor._disable_high_freq_reporting.assert_not_called()


def test_online_status_restores_reporting_cadence_and_closes_mqtt(capsys):
    device_key = "ONLINE-E3800"
    monitor = _make_monitor(
        devices=[_device(device_key, "E3800LFP", "Online")],
        latest_data={
            device_key: {
                "battery_percentage": 50,
                "battery_temp": 20,
                "host_packet_data_jdb": {
                    "host_packet_voltage": 50,
                    "host_packet_current": 0,
                },
            }
        },
        offline=False,
    )
    mqtt_client = MagicMock()
    monitor.mqtt_client = mqtt_client

    _, sleep = _run_status(monitor, capsys, force_offline=False)

    monitor.connect_mqtt.assert_called_once_with()
    monitor._enable_high_freq_reporting.assert_called_once_with(stagger=1)
    monitor._disable_high_freq_reporting.assert_called_once_with()
    mqtt_client.loop_stop.assert_called_once_with()
    mqtt_client.disconnect.assert_called_once_with()
    assert sleep.call_args_list == [call(3), call(2), call(5)]
