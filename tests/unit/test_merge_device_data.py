"""Merging partial packets into latest_data must not freeze switches ON."""

from monitor import PecronMonitor


DK = "AABBCCDDEEFF"


def make_monitor(controls=None):
    monitor = PecronMonitor({"region": "na"})
    monitor.devices = [{"device_key": DK, "product_key": "p11u2b", "controls": controls or {}}]
    return monitor


def test_switch_turning_off_is_recorded():
    monitor = make_monitor()
    monitor._merge_device_data(DK, {"ac_switch_hm": True, "dc_switch_hm": True})

    monitor._merge_device_data(DK, {"ac_switch_hm": False})

    assert monitor.latest_data[DK] == {"ac_switch_hm": False, "dc_switch_hm": True}


def test_bool_control_reported_as_integer_zero_is_recorded():
    monitor = make_monitor({"ac_switch_hm": {"id": 56, "type": "BOOL", "access": "RW"}})
    monitor._merge_device_data(DK, {"ac_switch_hm": 1})

    monitor._merge_device_data(DK, {"ac_switch_hm": 0})

    assert monitor.latest_data[DK]["ac_switch_hm"] == 0


def test_nested_switch_turning_off_is_recorded():
    monitor = make_monitor()
    monitor._merge_device_data(DK, {"host_packet_data_jdb": {"host_packet_ac_switch": True}})

    monitor._merge_device_data(DK, {"host_packet_data_jdb": {"host_packet_ac_switch": False}})

    assert monitor.latest_data[DK]["host_packet_data_jdb"]["host_packet_ac_switch"] is False


def test_numeric_zero_placeholders_still_keep_earlier_readings():
    # E3800 partial packets carry 0 for fields they don't report.
    monitor = make_monitor()
    monitor._merge_device_data(DK, {"battery_voltage": 52.8, "battery_percentage": 80})

    monitor._merge_device_data(DK, {"battery_voltage": 0, "battery_percentage": 79})

    assert monitor.latest_data[DK] == {"battery_voltage": 52.8, "battery_percentage": 79}
