"""Behavioral contracts for the Home Assistant MQTT connection boundary."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

import ha_bridge as ha_bridge_module
from ha_bridge import HomeAssistantBridge


class RecordingClient:
    """Small paho double that preserves externally visible call ordering."""

    def __init__(self, events, connect_error=None):
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "connect_error", connect_error)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name in {"on_connect", "on_disconnect", "on_message"}:
            self.events.append(("callback", name))

    def username_pw_set(self, username, password):
        self.events.append(("username_pw_set", username, password))

    def reconnect_delay_set(self, *, min_delay, max_delay):
        self.events.append(("reconnect_delay_set", min_delay, max_delay))

    def connect(self, host, port):
        self.events.append(("connect", host, port))
        if self.connect_error is not None:
            raise self.connect_error

    def loop_start(self):
        self.events.append(("loop_start",))

    def loop_stop(self):
        self.events.append(("loop_stop",))

    def disconnect(self):
        self.events.append(("disconnect",))

    def subscribe(self, topic, qos):
        self.events.append(("subscribe", topic, qos))


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


def _device():
    return {"device_key": "DEV1", "device_name": "E1500LFP", "controls": {}}


def _bridge(config=None):
    return HomeAssistantBridge(config or {}, devices=[_device()])


def _install_mqtt(monkeypatch, client, events, accepted=0):
    api_version = object()

    def client_factory(**kwargs):
        events.append(("Client", kwargs))
        return client

    fake_mqtt = SimpleNamespace(
        Client=client_factory,
        CallbackAPIVersion=SimpleNamespace(VERSION2=api_version),
        CONNACK_ACCEPTED=accepted,
    )
    monkeypatch.setattr(ha_bridge_module, "mqtt", fake_mqtt)
    return api_version


def _configured_client(monkeypatch, config=None):
    events = []
    client = RecordingClient(events)
    _install_mqtt(monkeypatch, client, events)
    bridge = _bridge(config)
    bridge._connect_attempt()
    return bridge, client, events


def test_constructor_defaults_leave_connection_and_energy_state_inert():
    devices = [_device()]

    bridge = HomeAssistantBridge({}, devices)

    assert bridge.ha_config == {}
    assert bridge.devices is devices
    assert bridge.client is None
    assert bridge.discovery_prefix == "homeassistant"
    assert bridge._connected is False
    assert bridge._last_retry_at == 0.0
    assert bridge._retry_interval == 60
    assert bridge.clear_discovery_on_startup is True
    assert bridge._clear_current_discovery is False
    assert bridge.energy_sensors is False
    assert bridge._energy is None
    assert bridge._device_dev_info == {}
    assert bridge._deferred_ports_published == set()
    assert bridge._state_cache == {}
    assert bridge._last_state == {}
    assert bridge._command_topics == []


def test_constructor_energy_opt_in_uses_configured_integrator_settings(monkeypatch):
    integrator = object()
    integrator_factory = MagicMock(return_value=integrator)
    monkeypatch.setattr(ha_bridge_module, "EnergyIntegrator", integrator_factory)

    bridge = HomeAssistantBridge(
        {
            "discovery_prefix": "custom-ha",
            "retry_interval": 17,
            "clear_discovery_on_startup": False,
            "energy_sensors": "enabled",
            "energy_state_path": "/tmp/ha-energy.json",
            "energy_max_gap_seconds": 321,
        },
        [_device()],
    )

    assert bridge.discovery_prefix == "custom-ha"
    assert bridge._retry_interval == 17
    assert bridge.clear_discovery_on_startup is False
    assert bridge.energy_sensors is True
    assert bridge._energy is integrator
    integrator_factory.assert_called_once_with(
        configured_path="/tmp/ha-energy.json", max_gap_seconds=321
    )


def test_connect_records_attempt_timestamp_before_delegating(monkeypatch):
    bridge = _bridge()
    events = []
    monkeypatch.setattr(ha_bridge_module.time, "time", lambda: events.append("time") or 123.5)
    bridge._connect_attempt = MagicMock(side_effect=lambda: events.append("attempt"))

    assert bridge.connect() is None

    assert bridge._last_retry_at == 123.5
    assert events == ["time", "attempt"]
    bridge._connect_attempt.assert_called_once_with()


def test_connect_attempt_configures_paho_credentials_callbacks_and_loop_in_exact_order(
    monkeypatch,
):
    config = {
        "mqtt_host": "broker.internal",
        "mqtt_port": 2883,
        "mqtt_user": "ha-user",
        "mqtt_password": "ha-password",
        "retry_interval": 47,
    }
    events = []
    client = RecordingClient(events)
    api_version = _install_mqtt(monkeypatch, client, events)
    bridge = _bridge(config)

    bridge._connect_attempt()

    assert bridge.client is client
    assert events == [
        (
            "Client",
            {
                "client_id": "pecron_ha_bridge",
                "callback_api_version": api_version,
            },
        ),
        ("username_pw_set", "ha-user", "ha-password"),
        ("callback", "on_connect"),
        ("callback", "on_disconnect"),
        ("callback", "on_message"),
        ("reconnect_delay_set", 1, 47),
        ("connect", "broker.internal", 2883),
        ("loop_start",),
    ]
    assert callable(client.on_connect)
    assert callable(client.on_disconnect)
    assert callable(client.on_message)


def test_connect_attempt_uses_broker_defaults_and_omits_empty_credentials(monkeypatch):
    bridge, client, events = _configured_client(monkeypatch)

    assert bridge.client is client
    assert not any(event[0] == "username_pw_set" for event in events)
    assert ("connect", "localhost", 1883) in events


def test_connect_attempt_tears_down_old_client_before_constructing_replacement(monkeypatch):
    bridge = _bridge()
    old_client = MagicMock()
    bridge.client = old_client
    events = []
    new_client = RecordingClient(events)
    _install_mqtt(monkeypatch, new_client, events)

    bridge._connect_attempt()

    assert old_client.method_calls == [call.loop_stop(), call.disconnect()]
    assert bridge.client is new_client
    assert events[0][0] == "Client"


def test_connect_attempt_continues_when_old_client_teardown_raises(monkeypatch):
    bridge = _bridge()
    old_client = MagicMock()
    old_client.loop_stop.side_effect = RuntimeError("stale loop")
    bridge.client = old_client
    events = []
    new_client = RecordingClient(events)
    _install_mqtt(monkeypatch, new_client, events)

    bridge._connect_attempt()

    old_client.loop_stop.assert_called_once_with()
    old_client.disconnect.assert_not_called()
    assert bridge.client is new_client
    assert ("loop_start",) in events


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError("refused"), OSError("route unavailable")],
    ids=["connection-refused", "os-error"],
)
def test_connect_attempt_contains_broker_connection_failures(monkeypatch, caplog, error):
    events = []
    client = RecordingClient(events, connect_error=error)
    _install_mqtt(monkeypatch, client, events)
    bridge = _bridge({"mqtt_host": "offline-broker", "mqtt_port": 1884, "retry_interval": 23})
    bridge._connected = True

    with caplog.at_level(logging.ERROR, logger="pecron"):
        assert bridge._connect_attempt() is None

    assert bridge.client is None
    assert bridge._connected is False
    assert ("loop_start",) not in events
    assert caplog.messages == [
        f"Cannot connect to MQTT broker at offline-broker:1884 ({error}). Will retry every 23s."
    ]


def test_accepted_connect_callback_publishes_then_subscribes_discovered_commands(
    monkeypatch,
):
    bridge, client, events = _configured_client(monkeypatch)

    def publish_discovery():
        events.append(("publish_discovery",))
        bridge._command_topics = [
            "pecron/DEV1/ac/set",
            "pecron/DEV1/eco_mode/set",
        ]

    bridge._publish_discovery = MagicMock(side_effect=publish_discovery)
    events.clear()

    client.on_connect(client, None, {}, 0)

    assert bridge._connected is True
    bridge._publish_discovery.assert_called_once_with()
    assert events == [
        ("publish_discovery",),
        ("subscribe", "pecron/DEV1/ac/set", 1),
        ("subscribe", "pecron/DEV1/eco_mode/set", 1),
    ]


def test_rejected_connect_callback_does_not_publish_or_subscribe(monkeypatch):
    bridge, client, events = _configured_client(monkeypatch)
    bridge._publish_discovery = MagicMock()
    bridge._command_topics = ["pecron/DEV1/ac/set"]
    events.clear()

    client.on_connect(client, None, {}, 5)

    assert bridge._connected is False
    bridge._publish_discovery.assert_not_called()
    assert events == []


@pytest.mark.parametrize("was_connected", [True, False])
def test_disconnect_callback_clears_connection_and_only_warns_for_live_session(
    monkeypatch, caplog, was_connected
):
    bridge, client, _events = _configured_client(monkeypatch)
    bridge._connected = was_connected

    with caplog.at_level(logging.WARNING, logger="pecron"):
        client.on_disconnect(client, None, {}, 7)

    assert bridge._connected is False
    expected = ["Home Assistant MQTT bridge disconnected (rc=7)"] if was_connected else []
    assert caplog.messages == expected


def test_message_callback_dispatches_four_part_set_topic_with_uppercase_payload(monkeypatch):
    bridge, client, _events = _configured_client(monkeypatch)
    bridge._handle_command = MagicMock()

    client.on_message(client, None, FakeMessage("pecron/DEV1/ac/set", b"on"))

    bridge._handle_command.assert_called_once_with("DEV1", "ac", "ON")


@pytest.mark.parametrize(
    "topic",
    [
        "pecron/DEV1/ac/state",
        "pecron/DEV1/set",
        "pecron/DEV1/ac/extra/set",
    ],
)
def test_message_callback_ignores_topics_outside_command_shape(monkeypatch, topic):
    bridge, client, _events = _configured_client(monkeypatch)
    bridge._handle_command = MagicMock()

    client.on_message(client, None, FakeMessage(topic, b"ON"))

    bridge._handle_command.assert_not_called()


def test_try_reconnect_guards_connected_and_background_client_without_reading_clock(monkeypatch):
    bridge = _bridge()
    bridge._connect_attempt = MagicMock()
    monkeypatch.setattr(
        ha_bridge_module.time,
        "time",
        MagicMock(side_effect=AssertionError("guard must return before reading clock")),
    )

    bridge._connected = True
    assert bridge.try_reconnect() is False

    bridge._connected = False
    bridge.client = object()
    assert bridge.try_reconnect() is False
    bridge._connect_attempt.assert_not_called()


def test_try_reconnect_waits_below_boundary_then_runs_at_exact_boundary(monkeypatch, caplog):
    bridge = _bridge({"retry_interval": 60})
    bridge._last_retry_at = 100.0
    bridge._connect_attempt = MagicMock()
    now = MagicMock(return_value=159.999)
    monkeypatch.setattr(ha_bridge_module.time, "time", now)

    assert bridge.try_reconnect() is False
    assert bridge._last_retry_at == 100.0
    bridge._connect_attempt.assert_not_called()

    now.return_value = 160.0
    with caplog.at_level(logging.INFO, logger="pecron"):
        assert bridge.try_reconnect() is True

    assert bridge._last_retry_at == 160.0
    bridge._connect_attempt.assert_called_once_with()
    assert "Retrying Home Assistant MQTT connection..." in caplog.messages


def test_handle_command_is_optional_and_delegates_exact_arguments_when_present():
    bridge = _bridge()

    assert bridge._handle_command("DEV1", "ac", "ON") is None

    bridge.command_callback = MagicMock()
    assert bridge._handle_command("DEV1", "dc", "OFF") is None
    bridge.command_callback.assert_called_once_with("DEV1", "dc", "OFF")


def test_disconnect_stops_loop_then_disconnects_without_replacing_client():
    events = []
    client = RecordingClient(events)
    bridge = _bridge()
    bridge.client = client
    bridge._connected = True

    assert bridge.disconnect() is None

    assert events == [("loop_stop",), ("disconnect",)]
    assert bridge.client is client
    assert bridge._connected is True


def test_disconnect_without_client_is_a_no_op():
    bridge = _bridge()

    assert bridge.disconnect() is None
    assert bridge.client is None
