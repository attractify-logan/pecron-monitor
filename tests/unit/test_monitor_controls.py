"""Characterization tests for PecronMonitor control routing."""

import logging
from unittest.mock import MagicMock, call, patch

import pytest

from constants import DEFAULT_CONTROLS
from monitor import PecronMonitor


DEVICE_KEY = "device-key"
PRODUCT_KEY = "product-key"
CHANNEL_ID = "qdproduct-keydevice-key"
TOKEN_DATA = {"token": "cloud-token"}
OMITTED = object()


def make_monitor(controls=OMITTED):
    monitor = PecronMonitor({"region": "na"})
    device = {
        "device_key": DEVICE_KEY,
        "product_key": PRODUCT_KEY,
        "device_name": "Test Device",
    }
    if controls is not OMITTED:
        device["controls"] = controls
    monitor.devices = [device]
    return monitor


def transport(connected, send_result=True, send_error=None):
    mock = MagicMock()
    mock.connected = connected
    if send_error is not None:
        mock.send_control.side_effect = send_error
    else:
        mock.send_control.return_value = send_result
    return mock


def test_missing_device_is_rejected_before_packet_or_transport(caplog):
    monitor = make_monitor()
    monitor.devices = []
    monitor.mqtt_client = MagicMock()

    with (
        patch("monitor_controls.build_ttlv_write_bool") as build_bool,
        patch("monitor_controls.build_ttlv_write_enum") as build_enum,
        patch("monitor_controls.set_device_property_rest") as rest,
        caplog.at_level(logging.ERROR, logger="pecron"),
    ):
        assert monitor.send_control("missing", "ac_switch_hm", True) is False

    assert monitor._packet_id == 0
    assert "Device missing not found" in caplog.text
    build_bool.assert_not_called()
    build_enum.assert_not_called()
    rest.assert_not_called()
    monitor.mqtt_client.publish.assert_not_called()


def test_missing_custom_control_is_rejected_without_using_defaults(caplog):
    monitor = make_monitor({})

    with (
        patch("monitor_controls.build_ttlv_write_bool") as build_bool,
        patch("monitor_controls.set_device_property_rest") as rest,
        caplog.at_level(logging.ERROR, logger="pecron"),
    ):
        assert monitor.send_control(DEVICE_KEY, "ac_switch_hm", True) is False

    assert monitor._packet_id == 0
    assert f"Control ac_switch_hm not found for device {DEVICE_KEY}" in caplog.text
    build_bool.assert_not_called()
    rest.assert_not_called()


@pytest.mark.parametrize("access", ["R", "r", "read"])
def test_read_only_control_is_rejected_case_insensitively(access, caplog):
    monitor = make_monitor({"setting": {"id": 90, "type": "BOOL", "access": access}})
    monitor.mqtt_client = MagicMock()

    with (
        patch("monitor_controls.build_ttlv_write_bool") as build_bool,
        patch("monitor_controls.set_device_property_rest") as rest,
        caplog.at_level(logging.ERROR, logger="pecron"),
    ):
        assert monitor.send_control(DEVICE_KEY, "setting", True) is False

    assert monitor._packet_id == 0
    assert f"Control setting is read-only (access={access.upper()})" in caplog.text
    build_bool.assert_not_called()
    rest.assert_not_called()
    monitor.mqtt_client.publish.assert_not_called()


def test_missing_access_defaults_to_read_only():
    monitor = make_monitor({"setting": {"id": 90, "type": "BOOL"}})

    with patch("monitor_controls.build_ttlv_write_bool") as build_bool:
        assert monitor.send_control(DEVICE_KEY, "setting", True) is False

    build_bool.assert_not_called()
    assert monitor._packet_id == 0


def test_default_controls_build_bool_packets_with_incrementing_ids_and_exact_mqtt_wire():
    monitor = make_monitor()
    monitor.mqtt_client = MagicMock()

    with patch(
        "monitor_controls.build_ttlv_write_bool", side_effect=[b"packet-one", b"packet-two"]
    ) as build:
        assert monitor.send_control(DEVICE_KEY, "ac_switch_hm", 1) is True
        assert monitor.send_control(DEVICE_KEY, "dc_switch_hm", 0) is True

    assert "controls" not in monitor.devices[0]
    assert DEFAULT_CONTROLS["ac_switch_hm"]["id"] == 40
    assert DEFAULT_CONTROLS["dc_switch_hm"]["id"] == 38
    assert monitor._packet_id == 2
    assert build.call_args_list == [call(1, 40, True), call(2, 38, False)]
    assert monitor.mqtt_client.publish.call_args_list == [
        call(f"q/1/d/{CHANNEL_ID}/bus", b"packet-one", qos=1),
        call(f"q/1/d/{CHANNEL_ID}/bus", b"packet-two", qos=1),
    ]


def test_custom_control_without_type_defaults_to_bool_and_propagates_verify():
    monitor = make_monitor({"custom": {"id": 731, "access": "rw"}})
    ble = transport(connected=True, send_result=True)
    monitor.ble_transports[DEVICE_KEY] = ble

    with patch("monitor_controls.build_ttlv_write_bool", return_value=b"custom-packet") as build:
        assert monitor.send_control(DEVICE_KEY, "custom", "enabled", verify=False) is True

    build.assert_called_once_with(1, 731, True)
    ble.send_control.assert_called_once_with(731, "enabled", "BOOL", verify=False)


def test_unknown_type_uses_bool_packet_but_skips_local_transports(caplog):
    monitor = make_monitor({"custom": {"id": 732, "type": "future-type", "access": "RW"}})
    ble = transport(connected=True)
    tcp = transport(connected=True)
    monitor.ble_transports[DEVICE_KEY] = ble
    monitor.local_transports[DEVICE_KEY] = tcp
    monitor.mqtt_client = MagicMock()

    with (
        patch(
            "monitor_controls.build_ttlv_write_bool", return_value=b"fallback-bool"
        ) as build_bool,
        patch("monitor_controls.build_ttlv_write_enum") as build_enum,
        caplog.at_level(logging.WARNING, logger="pecron"),
    ):
        assert monitor.send_control(DEVICE_KEY, "custom", 2, verify=False) is True

    build_bool.assert_called_once_with(1, 732, True)
    build_enum.assert_not_called()
    ble.send_control.assert_not_called()
    tcp.send_control.assert_not_called()
    monitor.mqtt_client.publish.assert_called_once_with(
        f"q/1/d/{CHANNEL_ID}/bus", b"fallback-bool", qos=1
    )
    assert "Unknown control type 'FUTURE-TYPE' for custom, trying bool" in caplog.text


def test_bool_ble_success_short_circuits_every_later_route_and_forwards_verify():
    monitor = make_monitor({"switch": {"id": 38, "type": "bool", "access": "rw"}})
    events = MagicMock()
    ble = transport(connected=True)
    ble.send_control.side_effect = lambda *args, **kwargs: (events.ble(*args, **kwargs), True)[1]
    tcp = transport(connected=True)
    monitor.ble_transports[DEVICE_KEY] = ble
    monitor.local_transports[DEVICE_KEY] = tcp
    monitor.mqtt_client = MagicMock()
    monitor.token_data = TOKEN_DATA

    def build_packet(*args):
        events.builder(*args)
        return b"wire"

    with (
        patch("monitor_controls.build_ttlv_write_bool", side_effect=build_packet),
        patch("monitor_controls.set_device_property_rest") as rest,
    ):
        assert monitor.send_control(DEVICE_KEY, "switch", False, verify=False) is True

    assert events.mock_calls == [
        call.builder(1, 38, False),
        call.ble(38, False, "BOOL", verify=False),
    ]
    tcp.send_control.assert_not_called()
    monitor.mqtt_client.publish.assert_not_called()
    rest.assert_not_called()


def test_bool_ble_failure_reconnects_tcp_then_tcp_success_short_circuits_cloud():
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})
    events = MagicMock()
    ble = transport(connected=True)
    ble.send_control.side_effect = lambda *args, **kwargs: (events.ble(*args, **kwargs), False)[1]
    tcp = transport(connected=False)
    tcp.send_control.side_effect = lambda *args, **kwargs: (events.tcp(*args, **kwargs), True)[1]
    monitor.ble_transports[DEVICE_KEY] = ble
    monitor.local_transports[DEVICE_KEY] = tcp
    monitor.mqtt_client = MagicMock()
    monitor.token_data = TOKEN_DATA

    def reconnect(device_key):
        events.reconnect(device_key)
        tcp.connected = True
        return True

    monitor._connect_local = MagicMock(side_effect=reconnect)

    def build_packet(*args):
        events.builder(*args)
        return b"wire"

    with (
        patch("monitor_controls.build_ttlv_write_bool", side_effect=build_packet),
        patch("monitor_controls.set_device_property_rest") as rest,
    ):
        assert monitor.send_control(DEVICE_KEY, "switch", True) is True

    assert events.mock_calls == [
        call.builder(1, 38, True),
        call.ble(38, True, "BOOL", verify=True),
        call.reconnect(DEVICE_KEY),
        call.tcp(38, True, "BOOL", verify=True),
    ]
    monitor.mqtt_client.publish.assert_not_called()
    rest.assert_not_called()


def test_bool_ble_and_tcp_exceptions_fall_through_to_exact_mqtt_publish(caplog):
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})
    events = MagicMock()
    ble = transport(connected=True)
    tcp = transport(connected=True)

    def fail_ble(*args, **kwargs):
        events.ble(*args, **kwargs)
        raise RuntimeError("ble broke")

    def fail_tcp(*args, **kwargs):
        events.tcp(*args, **kwargs)
        raise ConnectionError("tcp broke")

    ble.send_control.side_effect = fail_ble
    tcp.send_control.side_effect = fail_tcp
    monitor.ble_transports[DEVICE_KEY] = ble
    monitor.local_transports[DEVICE_KEY] = tcp
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.side_effect = lambda *args, **kwargs: events.mqtt(*args, **kwargs)
    monitor.token_data = TOKEN_DATA

    def build_packet(*args):
        events.builder(*args)
        return b"wire"

    with (
        patch("monitor_controls.build_ttlv_write_bool", side_effect=build_packet),
        patch("monitor_controls.set_device_property_rest") as rest,
        caplog.at_level(logging.WARNING, logger="pecron"),
    ):
        assert monitor.send_control(DEVICE_KEY, "switch", True, verify=False) is True

    assert events.mock_calls == [
        call.builder(1, 38, True),
        call.ble(38, True, "BOOL", verify=False),
        call.tcp(38, True, "BOOL", verify=False),
        call.mqtt(f"q/1/d/{CHANNEL_ID}/bus", b"wire", qos=1),
    ]
    rest.assert_not_called()
    assert "BLE control failed: ble broke" in caplog.text
    assert "TCP control failed: tcp broke" in caplog.text


def test_mqtt_publish_exception_propagates_without_rest_fallback():
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.side_effect = RuntimeError("MQTT failed")
    monitor.token_data = TOKEN_DATA

    with (
        patch("monitor_controls.build_ttlv_write_bool", return_value=b"wire"),
        patch("monitor_controls.set_device_property_rest") as rest,
        pytest.raises(RuntimeError, match="MQTT failed"),
    ):
        monitor.send_control(DEVICE_KEY, "switch", True)

    monitor.mqtt_client.publish.assert_called_once_with(f"q/1/d/{CHANNEL_ID}/bus", b"wire", qos=1)
    rest.assert_not_called()


def test_control_packet_id_wraps_from_65534_to_zero():
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})
    monitor._packet_id = 65534
    monitor.mqtt_client = MagicMock()

    with patch("monitor_controls.build_ttlv_write_bool", return_value=b"wrapped-wire") as build:
        assert monitor.send_control(DEVICE_KEY, "switch", False) is True

    assert monitor._packet_id == 0
    build.assert_called_once_with(0, 38, False)
    monitor.mqtt_client.publish.assert_called_once_with(
        f"q/1/d/{CHANNEL_ID}/bus", b"wrapped-wire", qos=1
    )


def test_bool_tcp_reconnect_exception_falls_through_to_rest_when_mqtt_missing():
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})
    events = MagicMock()
    tcp = transport(connected=False)
    monitor.local_transports[DEVICE_KEY] = tcp
    monitor.token_data = TOKEN_DATA

    def fail_reconnect(device_key):
        events.reconnect(device_key)
        raise ConnectionError("offline")

    monitor._connect_local = MagicMock(side_effect=fail_reconnect)

    def build_packet(*args):
        events.builder(*args)
        return b"wire"

    def rest_success(*args):
        events.rest(*args)
        return True

    with (
        patch("monitor_controls.build_ttlv_write_bool", side_effect=build_packet),
        patch("monitor_controls.set_device_property_rest", side_effect=rest_success) as rest,
    ):
        assert monitor.send_control(DEVICE_KEY, "switch", False) is True

    assert events.mock_calls == [
        call.builder(1, 38, False),
        call.reconnect(DEVICE_KEY),
        call.rest(
            "cloud-token",
            monitor.region,
            PRODUCT_KEY,
            DEVICE_KEY,
            {"switch": False},
        ),
    ]
    tcp.send_control.assert_not_called()
    rest.assert_called_once()


def test_bool_failures_try_ble_then_tcp_then_rest_and_return_false():
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})
    events = MagicMock()
    ble = transport(connected=True)
    tcp = transport(connected=True)
    ble.send_control.side_effect = lambda *args, **kwargs: (events.ble(*args, **kwargs), False)[1]
    tcp.send_control.side_effect = lambda *args, **kwargs: (events.tcp(*args, **kwargs), False)[1]
    monitor.ble_transports[DEVICE_KEY] = ble
    monitor.local_transports[DEVICE_KEY] = tcp
    monitor.token_data = TOKEN_DATA

    def build_packet(*args):
        events.builder(*args)
        return b"wire"

    def rest_failure(*args):
        events.rest(*args)
        return False

    with (
        patch("monitor_controls.build_ttlv_write_bool", side_effect=build_packet),
        patch("monitor_controls.set_device_property_rest", side_effect=rest_failure),
    ):
        assert monitor.send_control(DEVICE_KEY, "switch", True) is False

    assert events.mock_calls == [
        call.builder(1, 38, True),
        call.ble(38, True, "BOOL", verify=True),
        call.tcp(38, True, "BOOL", verify=True),
        call.rest(
            "cloud-token",
            monitor.region,
            PRODUCT_KEY,
            DEVICE_KEY,
            {"switch": True},
        ),
    ]


def test_bool_with_no_transport_returns_false_after_building_packet(caplog):
    monitor = make_monitor({"switch": {"id": 38, "type": "BOOL", "access": "RW"}})

    with (
        patch("monitor_controls.build_ttlv_write_bool", return_value=b"wire") as build,
        patch("monitor_controls.set_device_property_rest") as rest,
        caplog.at_level(logging.DEBUG, logger="pecron"),
    ):
        assert monitor.send_control(DEVICE_KEY, "switch", True) is False

    build.assert_called_once_with(1, 38, True)
    rest.assert_not_called()
    assert "Cannot send control switch: no cloud transport available" in caplog.text


@pytest.mark.parametrize("control_type", ["ENUM", "int", "Long"])
def test_non_bool_types_bypass_local_and_use_rest_first(control_type):
    monitor = make_monitor({"level": {"id": 51, "type": control_type, "access": "RW"}})
    monitor.ble_transports[DEVICE_KEY] = transport(connected=True)
    monitor.local_transports[DEVICE_KEY] = transport(connected=True)
    monitor.mqtt_client = MagicMock()
    monitor.token_data = TOKEN_DATA
    events = MagicMock()

    def build_packet(*args):
        events.builder(*args)
        return b"enum-wire"

    def rest_success(*args):
        events.rest(*args)
        return True

    with (
        patch("monitor_controls.build_ttlv_write_enum", side_effect=build_packet) as build,
        patch("monitor_controls.build_ttlv_write_bool") as build_bool,
        patch("monitor_controls.set_device_property_rest", side_effect=rest_success),
    ):
        assert monitor.send_control(DEVICE_KEY, "level", "7", verify=False) is True

    assert events.mock_calls == [
        call.builder(1, 51, 7),
        call.rest(
            "cloud-token",
            monitor.region,
            PRODUCT_KEY,
            DEVICE_KEY,
            {"level": "7"},
        ),
    ]
    build.assert_called_once()
    build_bool.assert_not_called()
    monitor.ble_transports[DEVICE_KEY].send_control.assert_not_called()
    monitor.local_transports[DEVICE_KEY].send_control.assert_not_called()
    monitor.mqtt_client.publish.assert_not_called()


def test_non_bool_rest_failure_falls_back_to_exact_mqtt_wire():
    monitor = make_monitor({"level": {"id": 51, "type": "ENUM", "access": "RW"}})
    monitor.mqtt_client = MagicMock()
    monitor.token_data = TOKEN_DATA
    events = MagicMock()

    def build_packet(*args):
        events.builder(*args)
        return b"enum-wire"

    def rest_failure(*args):
        events.rest(*args)
        return False

    monitor.mqtt_client.publish.side_effect = lambda *args, **kwargs: events.mqtt(*args, **kwargs)

    with (
        patch("monitor_controls.build_ttlv_write_enum", side_effect=build_packet),
        patch("monitor_controls.set_device_property_rest", side_effect=rest_failure),
    ):
        assert monitor.send_control(DEVICE_KEY, "level", 4) is True

    assert events.mock_calls == [
        call.builder(1, 51, 4),
        call.rest(
            "cloud-token",
            monitor.region,
            PRODUCT_KEY,
            DEVICE_KEY,
            {"level": 4},
        ),
        call.mqtt(f"q/1/d/{CHANNEL_ID}/bus", b"enum-wire", qos=1),
    ]


def test_non_bool_rest_exception_propagates_without_mqtt_fallback():
    monitor = make_monitor({"level": {"id": 51, "type": "ENUM", "access": "RW"}})
    monitor.mqtt_client = MagicMock()
    monitor.token_data = TOKEN_DATA

    with (
        patch("monitor_controls.build_ttlv_write_enum", return_value=b"enum-wire"),
        patch(
            "monitor_controls.set_device_property_rest", side_effect=RuntimeError("REST failed")
        ) as rest,
        pytest.raises(RuntimeError, match="REST failed"),
    ):
        monitor.send_control(DEVICE_KEY, "level", 4)

    rest.assert_called_once_with(
        "cloud-token",
        monitor.region,
        PRODUCT_KEY,
        DEVICE_KEY,
        {"level": 4},
    )
    monitor.mqtt_client.publish.assert_not_called()


def test_aliases_forward_exact_control_codes_and_values():
    monitor = make_monitor()
    monitor.send_control = MagicMock(return_value="sent")

    assert monitor.send_bool_control(DEVICE_KEY, "custom", False) == "sent"
    monitor.send_control.assert_called_once_with(DEVICE_KEY, "custom", False)

    monitor.send_bool_control = MagicMock(side_effect=["ac", "dc", "ups"])
    assert monitor.set_ac(DEVICE_KEY, True) == "ac"
    assert monitor.set_dc(DEVICE_KEY, False) == "dc"
    assert monitor.set_ups(DEVICE_KEY, 1) == "ups"
    assert monitor.send_bool_control.call_args_list == [
        call(DEVICE_KEY, "ac_switch_hm", True),
        call(DEVICE_KEY, "dc_switch_hm", False),
        call(DEVICE_KEY, "ups_status_hm", 1),
    ]
