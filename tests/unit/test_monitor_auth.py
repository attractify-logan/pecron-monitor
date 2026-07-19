"""Behavior characterization for monitor cloud authentication and recovery."""

from unittest.mock import MagicMock, call

import pytest

import monitor as monitor_module
import monitor_cloud as monitor_cloud_module
from monitor import PecronMonitor


TOKEN = {"token": "cloud-token", "uid": "user-1", "expires_at": 10_000.0}
CLOUD_DEVICE = {
    "product_key": "cloud-product",
    "device_key": "cloud-device",
    "device_name": "Cloud device",
    "product_name": "E1500LFP",
    "controls": {"cloud": True},
}


def _config(offline_capable=True):
    device = {
        "product_key": "cached-product",
        "device_key": "cached-device",
        "name": "Cached device",
        "tsl_cache": {"cached": True},
    }
    if offline_capable:
        device.update({"lan_ip": "192.0.2.10", "auth_key": "cached-auth-key"})
    return {
        "email": "owner@example.com",
        "password": "secret",
        "region": "na",
        "devices": [device],
        "cloud_retry_interval": 300,
        "mqtt_reconnect_interval": 60,
    }


def _monitor(config=None):
    monitor = PecronMonitor.__new__(PecronMonitor)
    monitor.config = config or _config()
    monitor.region = monitor_module.REGIONS[monitor.config["region"]]
    monitor.token_data = None
    monitor.mqtt_client = None
    monitor.devices = []
    monitor.offline_mode = False
    monitor.no_ble = False
    monitor.rest_only = False
    monitor.skip_local_setup = False
    monitor._fell_back_to_offline = False
    monitor._last_cloud_retry_at = 0.0
    monitor._mqtt_connect_failures = 0
    monitor._last_mqtt_rebuild_at = 0.0
    return monitor


def test_forced_offline_builds_cached_devices_then_sets_up_local(monkeypatch):
    monitor = _monitor()
    setup_local = MagicMock()
    monitor._setup_local_transports = setup_local
    login = MagicMock()
    resolve_devices = MagicMock()
    monkeypatch.setattr(monitor_cloud_module, "login", login)
    monkeypatch.setattr(monitor_cloud_module, "resolve_devices", resolve_devices)

    monitor.authenticate(force_offline=True)

    assert monitor.offline_mode is True
    assert monitor._fell_back_to_offline is False
    assert monitor.token_data is None
    assert monitor.devices == [
        {
            "product_key": "cached-product",
            "device_key": "cached-device",
            "device_name": "Cached device",
            "product_name": "Cached device",
            "controls": {"cached": True},
        }
    ]
    setup_local.assert_called_once_with()
    login.assert_not_called()
    resolve_devices.assert_not_called()


def test_forced_offline_rejects_incomplete_cached_config_before_setup(monkeypatch):
    monitor = _monitor(_config(offline_capable=False))
    monitor._setup_local_transports = MagicMock()
    login = MagicMock()
    monkeypatch.setattr(monitor_cloud_module, "login", login)

    with pytest.raises(RuntimeError, match="Cannot run in offline mode: missing required fields"):
        monitor.authenticate(force_offline=True)

    assert monitor.offline_mode is False
    assert monitor.devices == []
    monitor._setup_local_transports.assert_not_called()
    login.assert_not_called()


def test_normal_authentication_logs_in_resolves_and_sets_up_local_in_order(monkeypatch):
    monitor = _monitor(_config(offline_capable=False))
    events = []

    def fake_login(email, password, region):
        events.append(("login", email, password, region))
        return TOKEN

    def fake_resolve(config, token, region):
        events.append(("resolve", config, token, region))
        return [CLOUD_DEVICE]

    monitor._setup_local_transports = MagicMock(side_effect=lambda: events.append(("local",)))
    monkeypatch.setattr(monitor_cloud_module, "login", fake_login)
    monkeypatch.setattr(monitor_cloud_module, "resolve_devices", fake_resolve)

    monitor.authenticate()

    assert events == [
        ("login", "owner@example.com", "secret", monitor.region),
        ("resolve", monitor.config, "cloud-token", monitor.region),
        ("local",),
    ]
    assert monitor.token_data is TOKEN
    assert monitor.devices == [CLOUD_DEVICE]
    assert monitor.offline_mode is False
    assert monitor._fell_back_to_offline is False


def test_successful_reauthentication_replaces_fallback_session_state(monkeypatch):
    monitor = _monitor()
    old_token = {"token": "stale-token", "uid": "stale-user", "expires_at": 1.0}
    monitor.token_data = old_token
    monitor.devices = [{"device_key": "cached-device"}]
    monitor.offline_mode = True
    monitor._fell_back_to_offline = True
    monitor.skip_local_setup = True
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(return_value=TOKEN))
    monkeypatch.setattr(
        monitor_cloud_module, "resolve_devices", MagicMock(return_value=[CLOUD_DEVICE])
    )

    monitor.authenticate()

    assert monitor.token_data is TOKEN
    assert monitor.devices == [CLOUD_DEVICE]
    assert monitor.offline_mode is False
    assert monitor._fell_back_to_offline is False


def test_cloud_failure_falls_back_only_when_cached_config_is_offline_capable(monkeypatch):
    monitor = _monitor()
    monitor._setup_local_transports = MagicMock()
    failure = OSError("temporary DNS failure")
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(side_effect=failure))
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=1_234.0))

    monitor.authenticate()

    assert monitor.offline_mode is True
    assert monitor._fell_back_to_offline is True
    assert monitor._last_cloud_retry_at == 1_234.0
    assert [device["device_key"] for device in monitor.devices] == ["cached-device"]
    monitor._setup_local_transports.assert_called_once_with()


def test_cloud_failure_without_offline_capability_propagates_without_fallback(monkeypatch):
    monitor = _monitor(_config(offline_capable=False))
    monitor._setup_local_transports = MagicMock()
    failure = OSError("temporary DNS failure")
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(side_effect=failure))

    with pytest.raises(OSError, match="temporary DNS failure"):
        monitor.authenticate()

    assert monitor.offline_mode is False
    assert monitor._fell_back_to_offline is False
    assert monitor._last_cloud_retry_at == 0.0
    assert monitor.token_data is None
    assert monitor.devices == []
    monitor._setup_local_transports.assert_not_called()


def test_skip_local_persists_until_explicitly_changed_across_authentication(monkeypatch):
    monitor = _monitor(_config(offline_capable=False))
    monitor._setup_local_transports = MagicMock()
    login = MagicMock(return_value=TOKEN)
    resolve_devices = MagicMock(return_value=[CLOUD_DEVICE])
    monkeypatch.setattr(monitor_cloud_module, "login", login)
    monkeypatch.setattr(monitor_cloud_module, "resolve_devices", resolve_devices)

    monitor.authenticate(skip_local=True)
    monitor.authenticate()

    assert monitor.skip_local_setup is True
    monitor._setup_local_transports.assert_not_called()

    monitor.authenticate(skip_local=False)

    assert monitor.skip_local_setup is False
    monitor._setup_local_transports.assert_called_once_with()
    assert login.call_count == 3
    assert resolve_devices.call_count == 3


def test_rest_only_forces_skip_local_even_when_authentication_requests_it(monkeypatch):
    monitor = _monitor(_config(offline_capable=False))
    monitor.rest_only = True
    monitor._setup_local_transports = MagicMock()
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(return_value=TOKEN))
    monkeypatch.setattr(
        monitor_cloud_module, "resolve_devices", MagicMock(return_value=[CLOUD_DEVICE])
    )

    monitor.authenticate(skip_local=False)

    assert monitor.skip_local_setup is True
    monitor._setup_local_transports.assert_not_called()


def test_reauthentication_failure_retains_token_and_appends_cached_device(monkeypatch):
    monitor = _monitor()
    monitor.skip_local_setup = True
    login = MagicMock(side_effect=[TOKEN, OSError("cloud unavailable")])
    monkeypatch.setattr(monitor_cloud_module, "login", login)
    monkeypatch.setattr(
        monitor_cloud_module, "resolve_devices", MagicMock(return_value=[CLOUD_DEVICE])
    )
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=2_000.0))

    monitor.authenticate()
    monitor.authenticate()

    assert monitor.token_data is TOKEN
    assert [device["device_key"] for device in monitor.devices] == [
        "cloud-device",
        "cached-device",
    ]
    assert monitor.offline_mode is True
    assert monitor._fell_back_to_offline is True
    assert monitor._last_cloud_retry_at == 2_000.0


@pytest.mark.parametrize(
    ("offline_mode", "token_data", "now", "expected"),
    [
        (True, None, None, False),
        (False, None, None, True),
        (False, TOKEN, 9_700.0, False),
        (False, TOKEN, 9_700.001, True),
    ],
)
def test_token_refresh_uses_strict_300_second_boundary(
    monkeypatch, offline_mode, token_data, now, expected
):
    monitor = _monitor()
    monitor.offline_mode = offline_mode
    monitor.token_data = token_data
    time_mock = MagicMock(return_value=now)
    monkeypatch.setattr(monitor_cloud_module.time, "time", time_mock)

    assert monitor._token_needs_refresh() is expected
    if now is None:
        time_mock.assert_not_called()
    else:
        time_mock.assert_called_once_with()


def test_cloud_recovery_cooldown_blocks_until_exact_boundary(monkeypatch):
    monitor = _monitor()
    monitor.offline_mode = True
    monitor._fell_back_to_offline = True
    monitor._last_cloud_retry_at = 100.0
    monitor.skip_local_setup = True
    monitor.connect_mqtt = MagicMock()
    login = MagicMock(return_value=TOKEN)
    monkeypatch.setattr(monitor_cloud_module, "login", login)
    monkeypatch.setattr(
        monitor_cloud_module, "resolve_devices", MagicMock(return_value=[CLOUD_DEVICE])
    )
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(side_effect=[399.999, 400.0]))

    assert monitor._try_cloud_recovery() is False
    assert monitor._last_cloud_retry_at == 100.0
    login.assert_not_called()

    assert monitor._try_cloud_recovery() is True
    assert monitor._last_cloud_retry_at == 400.0
    login.assert_called_once_with("owner@example.com", "secret", monitor.region)


def test_cloud_recovery_phase_one_failure_preserves_session_state(monkeypatch):
    monitor = _monitor()
    old_token = {"token": "old-token", "uid": "old-user", "expires_at": 1.0}
    old_devices = [{"device_key": "old-device"}]
    monitor.token_data = old_token
    monitor.devices = old_devices
    monitor.offline_mode = True
    monitor._fell_back_to_offline = True
    monitor._last_cloud_retry_at = 100.0
    monitor._setup_local_transports = MagicMock()
    monitor.connect_mqtt = MagicMock()
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=400.0))
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(return_value=TOKEN))
    monkeypatch.setattr(monitor_cloud_module, "resolve_devices", MagicMock(return_value=[]))

    assert monitor._try_cloud_recovery() is False

    assert monitor._last_cloud_retry_at == 400.0
    assert monitor.token_data is old_token
    assert monitor.devices is old_devices
    assert monitor.offline_mode is True
    assert monitor._fell_back_to_offline is True
    monitor._setup_local_transports.assert_not_called()
    monitor.connect_mqtt.assert_not_called()


def test_cloud_recovery_commits_state_before_local_and_mqtt_setup(monkeypatch):
    monitor = _monitor()
    monitor.offline_mode = True
    monitor._fell_back_to_offline = True
    monitor._last_cloud_retry_at = 0.0
    events = []

    def observe_committed_state(stage):
        events.append(
            (
                stage,
                monitor.token_data,
                monitor.devices,
                monitor.offline_mode,
                monitor._fell_back_to_offline,
            )
        )

    monitor._setup_local_transports = MagicMock(
        side_effect=lambda: observe_committed_state("local")
    )
    monitor.connect_mqtt = MagicMock(side_effect=lambda: observe_committed_state("mqtt"))
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=300.0))
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(return_value=TOKEN))
    monkeypatch.setattr(
        monitor_cloud_module, "resolve_devices", MagicMock(return_value=[CLOUD_DEVICE])
    )

    assert monitor._try_cloud_recovery() is True

    expected_state = (TOKEN, [CLOUD_DEVICE], False, False)
    assert events == [
        ("local",) + expected_state,
        ("mqtt",) + expected_state,
    ]


def test_cloud_recovery_setup_failure_keeps_committed_state_and_reports_success(
    monkeypatch,
):
    monitor = _monitor()
    monitor.offline_mode = True
    monitor._fell_back_to_offline = True
    monitor._last_cloud_retry_at = 0.0
    setup_failure = RuntimeError("local setup failed")
    monitor._setup_local_transports = MagicMock(side_effect=setup_failure)
    monitor.connect_mqtt = MagicMock()
    log = MagicMock()
    monkeypatch.setattr(monitor_cloud_module, "log", log)
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=300.0))
    monkeypatch.setattr(monitor_cloud_module, "login", MagicMock(return_value=TOKEN))
    monkeypatch.setattr(
        monitor_cloud_module, "resolve_devices", MagicMock(return_value=[CLOUD_DEVICE])
    )

    assert monitor._try_cloud_recovery() is True

    assert monitor.token_data is TOKEN
    assert monitor.devices == [CLOUD_DEVICE]
    assert monitor.offline_mode is False
    assert monitor._fell_back_to_offline is False
    monitor.connect_mqtt.assert_not_called()
    log.warning.assert_called_once_with(
        "Cloud recovered but MQTT/local setup hit an error: %s", setup_failure
    )


def test_mqtt_rebuild_cooldown_preserves_failure_state(monkeypatch):
    monitor = _monitor()
    monitor._mqtt_connect_failures = 3
    monitor._last_mqtt_rebuild_at = 100.0
    monitor.mqtt_client = MagicMock()
    monitor.connect_mqtt = MagicMock()
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=159.999))

    assert monitor._recover_mqtt_connection() is False

    assert monitor._mqtt_connect_failures == 3
    assert monitor._last_mqtt_rebuild_at == 100.0
    monitor.mqtt_client.loop_stop.assert_not_called()
    monitor.mqtt_client.disconnect.assert_not_called()
    monitor.connect_mqtt.assert_not_called()


def test_mqtt_rebuild_failure_restores_failure_count_and_arms_cooldown(monkeypatch):
    monitor = _monitor()
    monitor._mqtt_connect_failures = 3
    monitor._last_mqtt_rebuild_at = 100.0
    monitor.mqtt_client = MagicMock()
    rebuild_failure = OSError("broker unavailable")
    monitor.connect_mqtt = MagicMock(side_effect=rebuild_failure)
    log = MagicMock()
    monkeypatch.setattr(monitor_cloud_module, "log", log)
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=160.0))

    assert monitor._recover_mqtt_connection() is False

    assert monitor._mqtt_connect_failures == 3
    assert monitor._last_mqtt_rebuild_at == 160.0
    assert monitor.mqtt_client.method_calls == [call.loop_stop(), call.disconnect()]
    monitor.connect_mqtt.assert_called_once_with()
    log.warning.assert_has_calls(
        [
            call("Rebuilding MQTT client after %d connection failure(s)", 3),
            call("MQTT rebuild failed: %s", rebuild_failure),
        ]
    )


def test_mqtt_rebuild_success_clears_failures_at_cooldown_boundary(monkeypatch):
    monitor = _monitor()
    monitor._mqtt_connect_failures = 3
    monitor._last_mqtt_rebuild_at = 100.0
    monitor.mqtt_client = MagicMock()
    monitor.connect_mqtt = MagicMock()
    monkeypatch.setattr(monitor_cloud_module.time, "time", MagicMock(return_value=160.0))

    assert monitor._recover_mqtt_connection() is True

    assert monitor._mqtt_connect_failures == 0
    assert monitor._last_mqtt_rebuild_at == 160.0
    assert monitor.mqtt_client.method_calls == [call.loop_stop(), call.disconnect()]
    monitor.connect_mqtt.assert_called_once_with()
