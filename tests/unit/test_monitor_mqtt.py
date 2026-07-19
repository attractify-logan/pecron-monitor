"""Behavioral contracts for PecronMonitor's cloud MQTT boundary."""

import json
import logging
from unittest.mock import MagicMock, call

import pytest

import monitor_cloud as monitor_module
from monitor import PecronMonitor


class FakeClient:
    """Small paho client double that records externally visible call ordering."""

    def __init__(self, events):
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "subscriptions", [])

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name in {"on_connect", "on_message"}:
            self.events.append(("callback", name))

    def ws_set_options(self, *, path):
        self.events.append(("ws_set_options", path))

    def tls_set(self):
        self.events.append(("tls_set",))

    def username_pw_set(self, *, username, password):
        self.events.append(("username_pw_set", username, password))

    def reconnect_delay_set(self, *, min_delay, max_delay):
        self.events.append(("reconnect_delay_set", min_delay, max_delay))

    def connect(self, host, port):
        self.events.append(("connect", host, port))

    def loop_start(self):
        self.events.append(("loop_start",))

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


def _message(suffix, payload, channel="channel"):
    return FakeMessage(f"q/2/d/{channel}/{suffix}", json.dumps(payload).encode("utf-8"))


def _monitor(make_config, **config_updates):
    config = make_config()
    config.update(config_updates)
    return PecronMonitor(config)


@pytest.mark.parametrize(
    ("offline_mode", "rest_only", "expected_log"),
    [
        (True, False, "Offline mode — skipping MQTT connection"),
        (False, True, "REST-only mode — skipping MQTT connection"),
    ],
)
def test_connect_mqtt_skips_cloud_client_in_offline_and_rest_modes(
    make_config, monkeypatch, caplog, offline_mode, rest_only, expected_log
):
    monitor = _monitor(make_config)
    monitor.offline_mode = offline_mode
    monitor.rest_only = rest_only
    existing_client = object()
    monitor.mqtt_client = existing_client

    def unexpected_client(**kwargs):
        raise AssertionError(f"paho client must not be constructed: {kwargs}")

    monkeypatch.setattr(monitor_module.mqtt, "Client", unexpected_client)

    with caplog.at_level(logging.INFO, logger="pecron"):
        assert monitor.connect_mqtt() is None

    assert monitor.mqtt_client is existing_client
    assert expected_log in caplog.messages


def test_connect_mqtt_configures_paho_client_and_starts_loop_in_exact_order(
    make_config, monkeypatch
):
    monitor = _monitor(make_config)
    monitor.token_data = {"uid": "user-42", "token": "cloud-token"}
    events = []
    fake_client = FakeClient(events)

    def client_factory(**kwargs):
        events.append(("Client", kwargs))
        return fake_client

    monkeypatch.setattr(monitor_module.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(monitor_module.mqtt, "Client", client_factory)

    monitor.connect_mqtt()

    assert monitor.mqtt_client is fake_client
    assert events == [
        (
            "Client",
            {
                "client_id": "qu_user-42_1700000000000",
                "transport": "websockets",
                "protocol": monitor_module.mqtt.MQTTv311,
                "callback_api_version": monitor_module.mqtt.CallbackAPIVersion.VERSION2,
            },
        ),
        ("ws_set_options", monitor.region["mqtt_path"]),
        ("tls_set",),
        ("username_pw_set", "", "cloud-token"),
        ("callback", "on_connect"),
        ("callback", "on_message"),
        ("reconnect_delay_set", 1, 60),
        ("connect", monitor.region["mqtt_host"], monitor.region["mqtt_port"]),
        ("loop_start",),
    ]
    assert fake_client.on_connect == monitor._on_connect
    assert fake_client.on_message == monitor._on_message


def test_on_connect_subscribes_each_device_topic_in_device_and_suffix_order(
    make_config, monkeypatch
):
    monitor = _monitor(make_config)
    monitor._mqtt_connect_failures = 4
    monitor.devices = [
        {"product_key": "PK1", "device_key": "DK1", "device_name": "First"},
        {"product_key": "PK2", "device_key": "DK2", "device_name": "Second"},
    ]
    client = FakeClient([])
    monkeypatch.setattr(monitor_module.mqtt, "CONNACK_ACCEPTED", 0)

    monitor._on_connect(client, None, {}, 0)

    assert monitor._mqtt_connect_failures == 0
    assert client.subscriptions == [
        ("q/2/d/qdPK1DK1/bus_", 1),
        ("q/2/d/qdPK1DK1/ack_", 1),
        ("q/2/d/qdPK1DK1/onl_", 1),
        ("q/2/d/qdPK2DK2/bus_", 1),
        ("q/2/d/qdPK2DK2/ack_", 1),
        ("q/2/d/qdPK2DK2/onl_", 1),
    ]


def test_on_connect_rejection_tracks_failure_without_subscribing(make_config, monkeypatch):
    monitor = _monitor(make_config)
    monitor.devices = [{"product_key": "PK", "device_key": "DK", "device_name": "Device"}]
    client = FakeClient([])
    monkeypatch.setattr(monitor_module.mqtt, "CONNACK_ACCEPTED", 0)
    monkeypatch.setattr(monitor_module.mqtt, "connack_string", lambda rc: f"failure {rc}")

    monitor._on_connect(client, None, {}, 5)

    assert monitor._mqtt_connect_failures == 1
    assert client.subscriptions == []


@pytest.mark.parametrize("payload", [b"not-json", b"\xff"])
def test_on_message_ignores_malformed_json_and_non_utf8(make_config, caplog, payload):
    monitor = _monitor(make_config)
    monitor._process_data = MagicMock()
    message = FakeMessage("q/2/d/channel/bus_", payload)

    with caplog.at_level(logging.DEBUG, logger="pecron"):
        monitor._on_message(None, None, message)

    monitor._process_data.assert_not_called()
    assert f"Non-JSON MQTT message on {message.topic} ({len(payload)} bytes)" in caplog.messages


def test_bus_message_merges_then_processes_accumulated_device_state(make_config):
    monitor = _monitor(make_config)
    monitor.latest_data = {"DK": {"voltage": 51.2, "output_power": 100}}
    monitor._local_data_keys = {"DK"}
    monitor._process_data = MagicMock()
    incoming = {
        "deviceKey": "DK",
        "data": {"kv": {"battery_percentage": 78, "output_power": 0}},
    }

    monitor._on_message(None, None, _message("bus_", incoming))

    assert monitor.latest_data["DK"] == {
        "voltage": 51.2,
        "output_power": 100,
        "battery_percentage": 78,
    }
    monitor._process_data.assert_called_once_with(
        "DK", monitor.latest_data["DK"], source="CLOUD MQTT"
    )


def test_empty_bus_message_does_not_merge_or_process(make_config):
    monitor = _monitor(make_config)
    monitor._merge_device_data = MagicMock()
    monitor._process_data = MagicMock()

    monitor._on_message(
        None,
        None,
        _message("bus_", {"deviceKey": "DK", "data": {"kv": {}}}),
    )

    monitor._merge_device_data.assert_not_called()
    monitor._process_data.assert_not_called()


@pytest.mark.parametrize(
    ("value", "online_calls", "offline_calls"),
    [(1, [call("DK")], []), (0, [], [call("DK")]), (2, [], [call("DK")])],
)
def test_online_message_routes_to_online_or_offline_restore_handler(
    make_config, value, online_calls, offline_calls
):
    monitor = _monitor(make_config)
    monitor._on_device_online = MagicMock()
    monitor._on_device_offline = MagicMock()

    monitor._on_message(
        None,
        None,
        _message("onl_", {"deviceKey": "DK", "data": {"value": value}}),
    )

    assert monitor._on_device_online.call_args_list == online_calls
    assert monitor._on_device_offline.call_args_list == offline_calls


def test_ack_message_is_logged_without_dispatch(make_config, caplog):
    monitor = _monitor(make_config)
    monitor._process_data = MagicMock()
    monitor._on_device_online = MagicMock()
    monitor._on_device_offline = MagicMock()

    with caplog.at_level(logging.DEBUG, logger="pecron"):
        monitor._on_message(None, None, _message("ack_", {"deviceKey": "DK"}))

    assert "ACK received for device DK" in caplog.messages
    monitor._process_data.assert_not_called()
    monitor._on_device_online.assert_not_called()
    monitor._on_device_offline.assert_not_called()


def test_code_4007_sets_state_and_emits_diagnostic_warning_only_once(make_config, caplog):
    monitor = _monitor(make_config)
    message = _message(
        "sys_",
        {"deviceKey": "DK", "code": 4007, "msg": "not bound", "type": "BUSI-ERROR"},
    )

    with caplog.at_level(logging.WARNING, logger="pecron"):
        monitor._on_message(None, None, message)
        monitor._on_message(None, None, message)

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert monitor._4007_warned is True
    assert len(warnings) == 5
    assert sum("code 4007" in warning for warning in warnings) == 1
    assert any("wrong product_key" in warning for warning in warnings)
    assert any("telemetry still arrives via MQTT/local/REST" in warning for warning in warnings)
    assert any(
        "persists AND the device never produces telemetry" in warning for warning in warnings
    )
    assert any("--diagnose -v" in warning and "--setup" in warning for warning in warnings)


def test_code_4026_warns_each_time_but_sets_state_and_emits_remediation_error_once(
    make_config, caplog
):
    monitor = _monitor(make_config, poll_interval=85)
    message = _message(
        "sys_",
        {
            "deviceKey": "DK",
            "code": 4026,
            "msg": "Insufficient resources",
            "type": "BUSI-ERROR",
        },
    )

    with caplog.at_level(logging.WARNING, logger="pecron"):
        monitor._on_message(None, None, message)
        monitor._on_message(None, None, message)

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert monitor._4026_warned is True
    assert warnings == [
        "Cloud system message: code=4026 msg='Insufficient resources' type=BUSI-ERROR",
        "Cloud system message: code=4026 msg='Insufficient resources' type=BUSI-ERROR",
    ]
    assert len(errors) == 1
    assert "per-account polling rate-limit (~1280 polls/day)" in errors[0]
    assert "Current poll_interval=85s" in errors[0]
    assert ">=70 recommended" in errors[0]
    assert "resets at 00:00 UTC" in errors[0]
