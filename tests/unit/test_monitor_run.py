"""Characterization tests for :meth:`PecronMonitor.run` lifecycle behavior."""

import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

from monitor import PecronMonitor


def make_monitor(make_config, *, poll_interval=60, warmup_seconds=None):
    config = make_config()
    config["poll_interval"] = poll_interval
    if warmup_seconds is not None:
        config["high_freq_warmup_seconds"] = warmup_seconds
    monitor = PecronMonitor(config)
    monitor.devices = [{"device_key": "device-1", "device_name": "E1500LFP"}]
    monitor.authenticate = MagicMock()
    monitor.connect_mqtt = MagicMock()
    monitor._run_init_rules = MagicMock()
    monitor._enable_high_freq_reporting = MagicMock()
    monitor._disable_high_freq_reporting = MagicMock()
    monitor._token_needs_refresh = MagicMock(return_value=False)
    monitor._try_cloud_recovery = MagicMock()
    monitor._recover_mqtt_connection = MagicMock()
    monitor._request_status_with_local_retries = MagicMock()
    return monitor


def stop_after_request(monitor, elapsed=0.0):
    def request(_poll_interval):
        monitor.stop()
        return elapsed

    return request


def test_cloud_poll_validation_happens_before_authentication(make_config):
    monitor = make_monitor(make_config, poll_interval=1)

    with patch("monitor_cloud.time.sleep") as sleep:
        with pytest.raises(ValueError, match=r"poll_interval=1s is below the \d+s floor"):
            monitor.run(force_offline=False)

    monitor.authenticate.assert_not_called()
    monitor.connect_mqtt.assert_not_called()
    sleep.assert_not_called()
    # run marks itself active before validating; this is current fail-fast state.
    assert monitor._running is True


def test_ha_wiring_and_startup_initialization_order(make_config):
    monitor = make_monitor(make_config, warmup_seconds=30)
    monitor.config["homeassistant"] = {
        "enabled": False,
        "host": "ha-broker.test",
        "port": 1883,
    }
    events = []

    monitor.authenticate.side_effect = lambda **kwargs: events.append(("authenticate", kwargs))
    monitor.connect_mqtt.side_effect = lambda: events.append(("connect_mqtt",))
    monitor._run_init_rules.side_effect = lambda: events.append(("init_rules",))
    monitor._request_status_with_local_retries.side_effect = stop_after_request(monitor)
    original_request = monitor._request_status_with_local_retries.side_effect

    def request(poll_interval):
        events.append(("request_status", poll_interval))
        return original_request(poll_interval)

    monitor._request_status_with_local_retries.side_effect = request

    class FakeBridge:
        def __init__(self, config, devices):
            events.append(("ha_construct", config, devices))
            self._command_callback = None

        @property
        def command_callback(self):
            return self._command_callback

        @command_callback.setter
        def command_callback(self, callback):
            assert callback.__self__ is monitor
            assert callback.__func__ is PecronMonitor._ha_command
            self._command_callback = callback
            events.append(("ha_callback",))

        def connect(self):
            events.append(("ha_connect",))

        def disconnect(self):
            events.append(("ha_disconnect",))

    fake_module = types.ModuleType("ha_bridge")
    fake_module.HomeAssistantBridge = FakeBridge

    def sleep(seconds):
        events.append(("sleep", seconds))

    with patch.dict(sys.modules, {"ha_bridge": fake_module}):
        with patch("monitor_cloud.time.sleep", side_effect=sleep):
            monitor.run(enable_ha=True, force_offline=True)

    assert events == [
        ("authenticate", {"force_offline": True}),
        ("connect_mqtt",),
        ("ha_construct", monitor.config["homeassistant"], monitor.devices),
        ("ha_callback",),
        ("ha_connect",),
        ("init_rules",),
        ("sleep", 3),
        ("request_status", 60),
        ("ha_disconnect",),
    ]
    assert monitor.ha_bridge.command_callback.__self__ is monitor
    assert monitor._running is False
    monitor._enable_high_freq_reporting.assert_not_called()


def test_disabled_ha_path_does_not_construct_bridge(make_config):
    monitor = make_monitor(make_config)
    monitor.config["homeassistant"] = {"enabled": True, "host": "unused"}
    monitor._request_status_with_local_retries.side_effect = stop_after_request(monitor)
    bridge_class = MagicMock()
    fake_module = types.ModuleType("ha_bridge")
    fake_module.HomeAssistantBridge = bridge_class

    with patch.dict(sys.modules, {"ha_bridge": fake_module}):
        with patch("monitor_cloud.time.sleep") as sleep:
            monitor.run(enable_ha=False, force_offline=True)

    bridge_class.assert_not_called()
    assert monitor.ha_bridge is None
    assert sleep.call_args_list == [call(3)]


def test_warmup_is_enabled_and_disabled_once_and_cycle_delay_uses_modulo(make_config):
    monitor = make_monitor(make_config, poll_interval=60, warmup_seconds=10)
    monitor.mqtt_client = MagicMock()
    request_results = iter((125.0, 120.0))

    def request(_poll_interval):
        try:
            return next(request_results)
        except StopIteration:
            monitor.stop()
            return 0.0

    monitor._request_status_with_local_retries.side_effect = request

    with patch("monitor_cloud.time.time", side_effect=(100.0, 110.0)):
        with patch("monitor_cloud.time.sleep") as sleep:
            monitor.run(force_offline=True)

    monitor._enable_high_freq_reporting.assert_called_once_with()
    monitor._disable_high_freq_reporting.assert_called_once_with()
    # 125 % 60 leaves 5s elapsed, while an exact 120s multiple waits a full cycle.
    assert sleep.call_args_list == [call(3), call(55.0), call(60)]
    assert monitor._request_status_with_local_retries.call_args_list == [
        call(60),
        call(60),
        call(60),
    ]
    monitor.mqtt_client.loop_stop.assert_called_once_with()
    monitor.mqtt_client.disconnect.assert_called_once_with()


def test_token_refresh_preserves_force_offline_and_reconnects_before_poll(make_config):
    monitor = make_monitor(make_config, warmup_seconds=0)
    transport = MagicMock()
    monitor.mqtt_client = transport
    events = []
    requests = 0

    monitor.authenticate.side_effect = lambda **kwargs: events.append(("authenticate", kwargs))
    monitor.connect_mqtt.side_effect = lambda: events.append(("connect_mqtt",))
    monitor._run_init_rules.side_effect = lambda: events.append(("init_rules",))
    monitor._token_needs_refresh.side_effect = lambda: events.append(("token_check",)) or True
    transport.loop_stop.side_effect = lambda: events.append(("mqtt_loop_stop",))
    transport.disconnect.side_effect = lambda: events.append(("mqtt_disconnect",))

    def request(poll_interval):
        nonlocal requests
        requests += 1
        events.append(("request_status", poll_interval))
        if requests == 2:
            monitor.stop()
        return 0.0

    monitor._request_status_with_local_retries.side_effect = request

    def sleep(seconds):
        events.append(("sleep", seconds))

    with patch("monitor_cloud.time.sleep", side_effect=sleep):
        monitor.run(force_offline=True)

    assert events == [
        ("authenticate", {"force_offline": True}),
        ("connect_mqtt",),
        ("init_rules",),
        ("sleep", 3),
        ("request_status", 60),
        ("sleep", 60),
        ("token_check",),
        ("mqtt_loop_stop",),
        ("mqtt_disconnect",),
        ("authenticate", {"force_offline": True}),
        ("connect_mqtt",),
        ("sleep", 3),
        ("request_status", 60),
        ("mqtt_loop_stop",),
        ("mqtt_disconnect",),
    ]
    assert monitor.authenticate.call_args_list == [
        call(force_offline=True),
        call(force_offline=True),
    ]
    monitor._try_cloud_recovery.assert_not_called()
    monitor._recover_mqtt_connection.assert_not_called()


def test_recovery_mqtt_reconnect_and_ha_retry_precede_next_poll(make_config):
    monitor = make_monitor(make_config, warmup_seconds=0)
    events = []
    requests = 0

    class Bridge:
        def try_reconnect(self):
            events.append("ha_retry")

        def disconnect(self):
            events.append("ha_disconnect")

    monitor.ha_bridge = Bridge()
    monitor._token_needs_refresh.side_effect = lambda: events.append("token_check") or False
    monitor._try_cloud_recovery.side_effect = lambda: events.append("cloud_recovery")
    monitor._recover_mqtt_connection.side_effect = lambda: events.append("mqtt_recovery")

    def request(_poll_interval):
        nonlocal requests
        requests += 1
        events.append("request_status")
        if requests == 2:
            monitor.stop()
        return 1.0

    monitor._request_status_with_local_retries.side_effect = request

    def sleep(seconds):
        events.append(("sleep", seconds))

    with patch("monitor_cloud.time.sleep", side_effect=sleep):
        monitor.run(force_offline=True)

    assert events == [
        ("sleep", 3),
        "request_status",
        ("sleep", 59.0),
        "token_check",
        "cloud_recovery",
        "mqtt_recovery",
        "ha_retry",
        "request_status",
        "ha_disconnect",
    ]


@pytest.mark.parametrize(
    ("error", "raises"),
    [(KeyboardInterrupt(), False), (RuntimeError("poll failed"), True)],
    ids=("keyboard-interrupt", "exception"),
)
def test_loop_exit_always_cleans_up_mqtt_and_ha(make_config, error, raises):
    monitor = make_monitor(make_config, warmup_seconds=0)
    events = []
    transport = MagicMock()
    monitor.mqtt_client = transport

    class Bridge:
        def try_reconnect(self):
            events.append("ha_retry")

        def disconnect(self):
            events.append("ha_disconnect")

    monitor.ha_bridge = Bridge()
    transport.loop_stop.side_effect = lambda: events.append("mqtt_loop_stop")
    transport.disconnect.side_effect = lambda: events.append("mqtt_disconnect")
    monitor._request_status_with_local_retries.side_effect = (0.0, error)

    def invoke():
        with patch("monitor_cloud.time.sleep"):
            monitor.run(force_offline=True)

    if raises:
        with pytest.raises(RuntimeError, match="poll failed"):
            invoke()
    else:
        invoke()

    assert events == [
        "ha_retry",
        "mqtt_loop_stop",
        "mqtt_disconnect",
        "ha_disconnect",
    ]
    assert monitor._running is False


def test_stop_only_clears_running_flag(make_config):
    monitor = PecronMonitor(make_config())
    monitor._running = True
    monitor.mqtt_client = MagicMock()
    monitor.ha_bridge = MagicMock()

    result = monitor.stop()

    assert result is None
    assert monitor._running is False
    monitor.mqtt_client.loop_stop.assert_not_called()
    monitor.mqtt_client.disconnect.assert_not_called()
    monitor.ha_bridge.disconnect.assert_not_called()
