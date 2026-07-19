"""Behavior characterization for PecronMonitor low-battery alerts."""

import json
import urllib.parse
from unittest.mock import MagicMock

import pytest

import monitor_alerts as monitor_module
from monitor import PecronMonitor


def _make_monitor(alerts=None, devices=None):
    monitor = PecronMonitor.__new__(PecronMonitor)
    monitor.config = {"alerts": alerts or {}}
    monitor.devices = devices or []
    monitor.last_alert = {}
    return monitor


@pytest.mark.parametrize("battery_pct", [20, 19, 0])
def test_low_battery_fires_at_and_below_threshold(monkeypatch, battery_pct):
    monitor = _make_monitor({"low_battery_percent": 20, "cooldown_minutes": 30})
    monitor._send_alert = MagicMock()
    time_mock = MagicMock(return_value=10_000.0)
    monkeypatch.setattr(monitor_module.time, "time", time_mock)

    monitor._check_alerts("device-a", battery_pct, 51.2, 125)

    time_mock.assert_called_once_with()
    assert monitor.last_alert == {"device-a": 10_000.0}
    monitor._send_alert.assert_called_once_with("device-a", battery_pct, 51.2, 125)


@pytest.mark.parametrize("battery_pct", [-1, -0.1, 20.1, 100])
def test_invalid_negative_and_above_threshold_values_do_not_alert(monkeypatch, battery_pct):
    monitor = _make_monitor({"low_battery_percent": 20, "cooldown_minutes": 30})
    monitor._send_alert = MagicMock()
    time_mock = MagicMock(return_value=10_000.0)
    monkeypatch.setattr(monitor_module.time, "time", time_mock)

    monitor._check_alerts("device-a", battery_pct, 51.2, 125)

    time_mock.assert_not_called()
    assert monitor.last_alert == {}
    monitor._send_alert.assert_not_called()


def test_cooldown_requires_elapsed_time_strictly_greater_than_boundary(monkeypatch):
    monitor = _make_monitor({"low_battery_percent": 20, "cooldown_minutes": 30})
    monitor.last_alert["device-a"] = 1_000.0
    monitor._send_alert = MagicMock()
    time_mock = MagicMock(side_effect=[2_800.0, 2_800.001])
    monkeypatch.setattr(monitor_module.time, "time", time_mock)

    monitor._check_alerts("device-a", 20, 51.2, 125)

    assert monitor.last_alert == {"device-a": 1_000.0}
    monitor._send_alert.assert_not_called()

    monitor._check_alerts("device-a", 20, 51.2, 125)

    assert monitor.last_alert == {"device-a": 2_800.001}
    monitor._send_alert.assert_called_once_with("device-a", 20, 51.2, 125)


def test_cooldown_isolated_per_device_in_shared_last_alert_map(monkeypatch):
    monitor = _make_monitor({"low_battery_percent": 20, "cooldown_minutes": 30})
    monitor.last_alert["device-a"] = 9_900.0
    monitor._send_alert = MagicMock()
    monkeypatch.setattr(monitor_module.time, "time", MagicMock(return_value=10_000.0))

    monitor._check_alerts("device-a", 10, 50.0, 60)
    monitor._check_alerts("device-b", 10, 50.0, 60)

    assert monitor.last_alert == {"device-a": 9_900.0, "device-b": 10_000.0}
    monitor._send_alert.assert_called_once_with("device-b", 10, 50.0, 60)


@pytest.mark.parametrize(
    ("devices", "device_key", "expected"),
    [
        ([{"device_key": "known", "device_name": "Garage Power"}], "known", "Garage Power"),
        ([{"device_key": "known", "name": "E1500LFP"}], "known", "E1500LFP"),
        ([{"device_key": "known"}], "known", "known"),
        ([{"device_key": "other", "device_name": "Other"}], "unknown", "unknown"),
    ],
)
def test_device_name_lookup_and_fallback(devices, device_key, expected):
    monitor = _make_monitor(devices=devices)

    assert monitor._get_device_name(device_key) == expected


def test_alert_message_and_destination_requests(monkeypatch):
    alerts = {
        "telegram": {
            "enabled": True,
            "bot_token": "bot-token:secret",
            "chat_id": "-12345",
        },
        "ntfy": {"enabled": True, "url": "https://ntfy.example/pecron"},
        "webhook": {"enabled": True, "url": "https://hooks.example/low-battery"},
    }
    monitor = _make_monitor(
        alerts,
        devices=[{"device_key": "device-a", "device_name": "Garage Power"}],
    )
    urlopen = MagicMock()
    log = MagicMock()
    monkeypatch.setattr(monitor_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(monitor_module, "log", log)

    monitor._send_alert("device-a", 7, 51.26, 125)

    message = (
        "⚠️ Pecron Low Battery Alert\n"
        "Device: Garage Power\n"
        "Battery: 7%\nVoltage: 51.3V\n"
        "Remaining: 2h 5m"
    )
    log.warning.assert_called_once_with(message)
    assert urlopen.call_count == 3

    telegram_request = urlopen.call_args_list[0].args[0]
    assert telegram_request.full_url == ("https://api.telegram.org/botbot-token:secret/sendMessage")
    assert telegram_request.get_method() == "POST"
    assert urllib.parse.parse_qs(telegram_request.data.decode()) == {
        "chat_id": ["-12345"],
        "text": [message],
    }
    assert urlopen.call_args_list[0].kwargs == {"timeout": 10}

    ntfy_request = urlopen.call_args_list[1].args[0]
    assert ntfy_request.full_url == "https://ntfy.example/pecron"
    assert ntfy_request.get_method() == "POST"
    assert ntfy_request.data == message.encode()
    assert ntfy_request.get_header("Title") == "Pecron Battery 7%"
    assert urlopen.call_args_list[1].kwargs == {"timeout": 10}

    webhook_request = urlopen.call_args_list[2].args[0]
    assert webhook_request.full_url == "https://hooks.example/low-battery"
    assert webhook_request.get_method() == "POST"
    assert webhook_request.get_header("Content-type") == "application/json"
    assert json.loads(webhook_request.data) == {
        "battery_percent": 7,
        "voltage": 51.26,
        "remain_minutes": 125,
        "device_key": "device-a",
        "message": message,
    }
    assert urlopen.call_args_list[2].kwargs == {"timeout": 10}


@pytest.mark.parametrize(
    ("failed_url", "error_label"),
    [
        ("api.telegram.org", "Telegram"),
        ("ntfy.example", "ntfy"),
        ("hooks.example", "Webhook"),
    ],
)
def test_each_destination_failure_is_logged_and_does_not_block_others(
    monkeypatch, failed_url, error_label
):
    monitor = _make_monitor(
        {
            "telegram": {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            },
            "ntfy": {"enabled": True, "url": "https://ntfy.example/topic"},
            "webhook": {"enabled": True, "url": "https://hooks.example/alert"},
        }
    )
    attempted_urls = []
    failure = OSError("destination unavailable")

    def fake_urlopen(request, timeout):
        assert timeout == 10
        attempted_urls.append(request.full_url)
        if failed_url in request.full_url:
            raise failure
        return MagicMock()

    log = MagicMock()
    monkeypatch.setattr(monitor_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(monitor_module, "log", log)

    monitor._send_alert("device-a", 5, 50.0, 30)

    assert attempted_urls == [
        "https://api.telegram.org/bottoken/sendMessage",
        "https://ntfy.example/topic",
        "https://hooks.example/alert",
    ]
    log.error.assert_called_once_with(f"{error_label} alert failed: %s", failure)


def test_failed_delivery_still_starts_cooldown_and_suppresses_repeat(monkeypatch):
    monitor = _make_monitor(
        {
            "low_battery_percent": 20,
            "cooldown_minutes": 1,
            "telegram": {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            },
        },
        devices=[{"device_key": "device-a", "device_name": "Garage Power"}],
    )
    time_mock = MagicMock(side_effect=[4_000.0, 4_001.0])
    urlopen = MagicMock(side_effect=OSError("offline"))
    log = MagicMock()
    monkeypatch.setattr(monitor_module.time, "time", time_mock)
    monkeypatch.setattr(monitor_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(monitor_module, "log", log)

    monitor._check_alerts("device-a", 5, 50.0, 30)
    monitor._check_alerts("device-a", 4, 49.9, 25)

    assert monitor.last_alert == {"device-a": 4_000.0}
    urlopen.assert_called_once()
    assert log.warning.call_count == 1
    log.error.assert_called_once_with("Telegram alert failed: %s", urlopen.side_effect)
