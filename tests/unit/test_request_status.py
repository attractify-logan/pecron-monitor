"""Behavioral contracts for PecronMonitor status polling and local fallback."""

from unittest.mock import MagicMock, call, patch

import pytest

from monitor import PecronMonitor


DEVICE_A = "AABBCCDDEEFF"
DEVICE_B = "112233445566"


def _device(device_key, product_key="p11u2b"):
    return {
        "product_key": product_key,
        "device_key": device_key,
        "device_name": "Test power station",
        "controls": {},
    }


def _monitor(make_config, device_keys=(DEVICE_A,), **kwargs):
    monitor = PecronMonitor(make_config(), **kwargs)
    monitor.devices = [_device(device_key) for device_key in device_keys]
    monitor._process_data = MagicMock()
    return monitor


def _publish_result():
    result = MagicMock()
    result.rc = 0
    result.mid = 7
    return result


def test_targeted_request_filters_devices_and_clears_only_their_cycle_marker(make_config):
    monitor = _monitor(make_config, (DEVICE_A, DEVICE_B))
    monitor._local_data_keys = {DEVICE_A, DEVICE_B, "unrelated-device"}
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.return_value = _publish_result()

    with patch("monitor_polling.build_ttlv_read", return_value=b"request-a") as build_read:
        monitor._request_status(device_keys={DEVICE_A})

    assert monitor._local_data_keys == {DEVICE_B, "unrelated-device"}
    build_read.assert_called_once_with(1)
    monitor.mqtt_client.publish.assert_called_once_with(
        "q/1/d/qdp11u2bAABBCCDDEEFF/bus", b"request-a", qos=1
    )


def test_full_request_clears_stale_cycle_markers_before_recording_fresh_local_data(make_config):
    monitor = _monitor(make_config, (DEVICE_A, DEVICE_B))
    monitor._local_data_keys = {DEVICE_A, DEVICE_B, "stale-device"}
    telemetry = {"battery_percentage": 70, "battery_temp": 21}
    ble = MagicMock()
    ble.connected = True
    ble.read_status.return_value = telemetry
    monitor.ble_transports[DEVICE_A] = ble

    monitor._request_status()

    assert monitor._local_data_keys == {DEVICE_A}
    monitor._process_data.assert_called_once_with(DEVICE_A, telemetry, source="BLE")


def test_ble_connects_reads_merges_then_processes_and_short_circuits_other_routes(make_config):
    monitor = _monitor(make_config)
    telemetry = {"battery_percentage": 74, "battery_temp": 22}
    events = []
    ble = MagicMock()
    ble.connected = False

    def connect():
        events.append("ble-connect")
        ble.connected = True

    ble.connect.side_effect = connect
    ble.read_status.side_effect = lambda: events.append("ble-read") or telemetry
    monitor.ble_transports[DEVICE_A] = ble
    monitor.local_transports[DEVICE_A] = MagicMock()
    monitor._connect_local = MagicMock()
    monitor.mqtt_client = MagicMock()
    original_merge = monitor._merge_device_data
    monitor._merge_device_data = MagicMock(
        side_effect=lambda device_key, kv: (
            events.append(("merge", device_key, kv)),
            original_merge(device_key, kv),
        )[1]
    )
    monitor._process_data.side_effect = lambda *args, **kwargs: events.append(
        ("process", args, kwargs)
    )

    monitor._request_status()

    assert events == [
        "ble-connect",
        "ble-read",
        ("merge", DEVICE_A, telemetry),
        ("process", (DEVICE_A, telemetry), {"source": "BLE"}),
    ]
    assert monitor.latest_data[DEVICE_A] == telemetry
    assert monitor._local_data_keys == {DEVICE_A}
    monitor._connect_local.assert_not_called()
    monitor.mqtt_client.publish.assert_not_called()


def test_ble_settings_only_payload_is_processed_but_still_short_circuits_other_routes(
    make_config,
):
    monitor = _monitor(make_config)
    settings = {"ac_output_voltage_io": 120, "battery_percentage": 80}
    ble = MagicMock()
    ble.connected = True
    ble.read_status.return_value = settings
    monitor.ble_transports[DEVICE_A] = ble
    monitor.local_transports[DEVICE_A] = MagicMock()
    monitor._connect_local = MagicMock()
    monitor.mqtt_client = MagicMock()

    monitor._request_status()

    assert monitor.latest_data[DEVICE_A] == settings
    assert DEVICE_A not in monitor._local_data_keys
    monitor._process_data.assert_called_once_with(DEVICE_A, settings, source="BLE")
    monitor._connect_local.assert_not_called()
    monitor.mqtt_client.publish.assert_not_called()


@pytest.mark.parametrize("failure_point", ["connect", "read"])
def test_ble_connect_and_read_exceptions_fall_through_to_tcp(make_config, failure_point):
    monitor = _monitor(make_config)
    tcp_data = {"battery_percentage": 63, "battery_temp": 20}
    ble = MagicMock()
    if failure_point == "connect":
        ble.connected = False
        ble.connect.side_effect = RuntimeError("BLE unavailable")
    else:
        ble.connected = True
        ble.read_status.side_effect = RuntimeError("BLE read failed")
    monitor.ble_transports[DEVICE_A] = ble

    local = MagicMock()
    local.connected = True
    local.read_status.return_value = tcp_data
    monitor.local_transports[DEVICE_A] = local
    monitor._connect_local = MagicMock(return_value=True)

    monitor._request_status()

    local.read_status.assert_called_once_with()
    monitor._process_data.assert_called_once_with(DEVICE_A, tcp_data, source="LOCAL TCP")
    assert DEVICE_A in monitor._local_data_keys


@pytest.mark.parametrize(
    ("payload", "is_telemetry"),
    [
        ({"battery_percentage": 55, "battery_temp": 19}, True),
        ({"battery_percentage": 55, "ac_output_voltage_io": 120}, False),
    ],
)
def test_tcp_marks_only_telemetry_local_but_always_publishes_mqtt(
    make_config, payload, is_telemetry
):
    monitor = _monitor(make_config)
    events = []
    local = MagicMock()
    local.connected = True
    local.read_status.return_value = payload
    monitor.local_transports[DEVICE_A] = local
    monitor._connect_local = MagicMock(return_value=True)
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.side_effect = lambda *args, **kwargs: (
        events.append(("publish", args, kwargs)) or _publish_result()
    )
    original_merge = monitor._merge_device_data
    monitor._merge_device_data = MagicMock(
        side_effect=lambda device_key, kv: (
            events.append(("merge", device_key, kv)),
            original_merge(device_key, kv),
        )[1]
    )
    monitor._process_data.side_effect = lambda *args, **kwargs: events.append(
        ("process", args, kwargs)
    )

    with patch("monitor_polling.build_ttlv_read", return_value=b"tcp-follow-up"):
        monitor._request_status()

    assert events == [
        ("merge", DEVICE_A, payload),
        ("process", (DEVICE_A, payload), {"source": "LOCAL TCP"}),
        (
            "publish",
            ("q/1/d/qdp11u2bAABBCCDDEEFF/bus", b"tcp-follow-up"),
            {"qos": 1},
        ),
    ]
    assert (DEVICE_A in monitor._local_data_keys) is is_telemetry
    assert monitor._local_connect_failures[DEVICE_A] == 0


def test_tcp_read_exception_falls_through_mqtt_then_rest(make_config):
    monitor = _monitor(make_config)
    monitor.token_data = {"token": "cloud-token"}
    events = []
    local = MagicMock()
    local.connected = True
    local.read_status.side_effect = lambda: (
        events.append("tcp-read") or (_ for _ in ()).throw(RuntimeError("socket closed"))
    )
    monitor.local_transports[DEVICE_A] = local
    monitor._connect_local = MagicMock(return_value=True)
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.side_effect = lambda *args, **kwargs: (
        events.append("mqtt-publish") or _publish_result()
    )
    rest_data = {"battery_percentage": 48}
    original_merge = monitor._merge_device_data
    monitor._merge_device_data = MagicMock(
        side_effect=lambda device_key, kv: (
            events.append("rest-merge"),
            original_merge(device_key, kv),
        )[1]
    )
    monitor._process_data.side_effect = lambda *args, **kwargs: events.append("rest-process")

    with patch("monitor_polling.build_ttlv_read", return_value=b"mqtt-frame"):
        with patch(
            "monitor_polling.get_device_properties_rest",
            side_effect=lambda *args: events.append("rest-fetch") or rest_data,
        ) as get_rest:
            monitor._request_status()

    assert events == [
        "tcp-read",
        "mqtt-publish",
        "rest-fetch",
        "rest-merge",
        "rest-process",
    ]
    get_rest.assert_called_once_with("cloud-token", monitor.region, "p11u2b", DEVICE_A)
    monitor._process_data.assert_called_once_with(DEVICE_A, rest_data, source="REST API")


def test_rest_only_fetches_and_processes_every_cycle_even_with_cached_data(make_config):
    monitor = _monitor(make_config, rest_only=True)
    monitor.token_data = {"token": "cloud-token"}
    monitor.latest_data[DEVICE_A] = {"cached": "value"}
    first = {"battery_percentage": 40}
    second = {"battery_percentage": 39}

    with patch(
        "monitor_polling.get_device_properties_rest", side_effect=[first, second]
    ) as get_rest:
        monitor._request_status()
        monitor._request_status()

    assert get_rest.call_args_list == [
        call("cloud-token", monitor.region, "p11u2b", DEVICE_A),
        call("cloud-token", monitor.region, "p11u2b", DEVICE_A),
    ]
    assert monitor._process_data.call_args_list == [
        call(DEVICE_A, first, source="REST API"),
        call(DEVICE_A, second, source="REST API"),
    ]


def test_non_rest_mode_uses_rest_only_until_the_device_has_cached_data(make_config):
    monitor = _monitor(make_config)
    monitor.token_data = {"token": "cloud-token"}
    rest_data = {"battery_percentage": 66}

    with patch("monitor_polling.get_device_properties_rest", return_value=rest_data) as get_rest:
        monitor._request_status()
        monitor._request_status()

    get_rest.assert_called_once_with("cloud-token", monitor.region, "p11u2b", DEVICE_A)
    monitor._process_data.assert_called_once_with(DEVICE_A, rest_data, source="REST API")


def test_mqtt_uses_device_topic_qos_built_frame_and_shared_packet_progression(make_config):
    monitor = _monitor(make_config, (DEVICE_A, DEVICE_B))
    monitor._packet_id = 40
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.return_value = _publish_result()

    with patch(
        "monitor_polling.build_ttlv_read",
        side_effect=lambda packet_id: f"frame-{packet_id}".encode(),
    ) as build_read:
        monitor._request_status()
        monitor._request_status()

    assert build_read.call_args_list == [call(41), call(42), call(43), call(44)]
    assert monitor.mqtt_client.publish.call_args_list == [
        call("q/1/d/qdp11u2bAABBCCDDEEFF/bus", b"frame-41", qos=1),
        call("q/1/d/qdp11u2b112233445566/bus", b"frame-42", qos=1),
        call("q/1/d/qdp11u2bAABBCCDDEEFF/bus", b"frame-43", qos=1),
        call("q/1/d/qdp11u2b112233445566/bus", b"frame-44", qos=1),
    ]
    assert monitor._packet_id == 44


def test_failed_auto_discovered_tcp_reconnects_after_threshold_and_reads_replacement(
    make_config,
):
    config = make_config(with_auth=True)
    monitor = PecronMonitor(config)
    monitor.devices = [_device(DEVICE_A)]
    monitor._process_data = MagicMock()
    old_transport = MagicMock()
    old_transport.connected = False
    replacement = MagicMock()
    replacement.connected = True
    telemetry = {"battery_percentage": 72, "battery_temp": 18}
    replacement.read_status.return_value = telemetry
    monitor.local_transports[DEVICE_A] = old_transport
    monitor._local_connect_failures[DEVICE_A] = 5
    monitor._connect_local = MagicMock(side_effect=[False, True])

    def rediscover(device_key):
        monitor.local_transports[device_key] = replacement
        return True

    monitor._rediscover_device = MagicMock(side_effect=rediscover)

    monitor._request_status()

    assert monitor._connect_local.call_args_list == [call(DEVICE_A), call(DEVICE_A)]
    monitor._rediscover_device.assert_called_once_with(DEVICE_A)
    replacement.read_status.assert_called_once_with()
    monitor._process_data.assert_called_once_with(DEVICE_A, telemetry, source="LOCAL TCP")


def test_failed_pinned_tcp_skips_rediscovery_and_falls_through_to_mqtt(make_config):
    monitor = PecronMonitor(make_config(with_lan=True, with_auth=True))
    monitor.devices = [_device(DEVICE_A)]
    monitor._process_data = MagicMock()
    local = MagicMock()
    local.connected = False
    monitor.local_transports[DEVICE_A] = local
    monitor._local_connect_failures[DEVICE_A] = 5
    monitor._connect_local = MagicMock(return_value=False)
    monitor._rediscover_device = MagicMock()
    monitor.mqtt_client = MagicMock()
    monitor.mqtt_client.publish.return_value = _publish_result()

    with patch("monitor_polling.build_ttlv_read", return_value=b"cloud-read"):
        monitor._request_status()

    monitor._rediscover_device.assert_not_called()
    monitor.mqtt_client.publish.assert_called_once_with(
        "q/1/d/qdp11u2bAABBCCDDEEFF/bus", b"cloud-read", qos=1
    )


def test_connect_local_tracks_failures_and_reconnects_fresh_after_cooldown(make_config):
    monitor = _monitor(make_config)
    local = MagicMock()
    local.connect.side_effect = [False, True]
    monitor.local_transports[DEVICE_A] = local

    with patch("monitor_polling.time.time", side_effect=[10.0, 12.0]):
        assert monitor._connect_local(DEVICE_A) is False
        assert monitor._local_connect_failures[DEVICE_A] == 1
        assert monitor._connect_local(DEVICE_A) is True

    assert local.connect.call_count == 2
    assert monitor._local_connect_failures[DEVICE_A] == 0
    assert monitor._last_connect_attempt[DEVICE_A] == 12.0


def test_connect_local_cooldown_and_exception_are_contained(make_config):
    monitor = _monitor(make_config)
    local = MagicMock()
    local.connect.side_effect = RuntimeError("connection refused")
    monitor.local_transports[DEVICE_A] = local

    with patch("monitor_polling.time.time", side_effect=[10.0, 10.5]):
        assert monitor._connect_local(DEVICE_A) is False
        assert monitor._connect_local(DEVICE_A) is False

    local.connect.assert_called_once_with()
    assert monitor._local_connect_failures[DEVICE_A] == 1


def test_rediscover_device_replaces_transport_and_preserves_model_read_timeout(
    make_config, fake_auth_key
):
    config = make_config(with_auth=True)
    monitor = PecronMonitor(config)
    monitor.devices = [_device(DEVICE_A)]
    replacement = MagicMock()

    with patch("monitor_polling.HAS_LOCAL", True):
        with patch("lan_scan.discover_devices", return_value={DEVICE_A: "192.0.2.25"}) as discover:
            with patch(
                "local_transport.LocalTransport", return_value=replacement
            ) as local_transport:
                assert monitor._rediscover_device(DEVICE_A) is True

    discover.assert_called_once_with(
        [{"device_key": DEVICE_A, "auth_key": fake_auth_key}], timeout=0.5
    )
    local_transport.assert_called_once_with(
        "192.0.2.25",
        fake_auth_key,
        device_key=DEVICE_A,
        controls={},
        multi_packet_timeout=3.0,
    )
    assert monitor.local_transports[DEVICE_A] is replacement
    assert config["devices"][0]["lan_ip"] == "192.0.2.25"
