"""Characterization tests for control probing and readback helpers."""

from unittest.mock import MagicMock, call, patch

import pytest

from monitor import PecronMonitor


DEVICE_KEY = "probe-device"
CONTROL_CODE = "probe_control"


def make_probe_monitor(make_config):
    monitor = PecronMonitor(make_config())
    monitor.devices = [
        {
            "device_key": DEVICE_KEY,
            "product_key": "probe-product",
            "controls": {CONTROL_CODE: {"type": "INT"}},
        }
    ]
    monitor.send_control = MagicMock()
    monitor._request_status = MagicMock()
    return monitor


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"wanted": "top", "nested": {"wanted": "nested"}}, "top"),
        ({"first": {"wanted": "first"}, "second": {"wanted": "second"}}, "first"),
        ({"items": [{"other": 1}, {"deeper": [{"wanted": 7}]}]}, 7),
        ({"first": {"wanted": None}, "second": {"wanted": "later"}}, "later"),
        ({"wanted": None, "nested": {"wanted": "not-used"}}, None),
        ({"nested": [{"other": 1}, {"still_other": 2}]}, None),
        ("not-a-container", None),
    ],
)
def test_extract_value_by_key_preserves_recursive_precedence(make_config, payload, expected):
    monitor = PecronMonitor(make_config())

    assert monitor._extract_value_by_key(payload, "wanted") == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, 1),
        (False, 0),
        (7, 7),
        (-4, -4),
        (8.99, 8),
        (-8.99, -8),
        (" ON ", 1),
        ("true", 1),
        ("Enabled", 1),
        (" off ", 0),
        ("FALSE", 0),
        ("disabled", 0),
        ("12", 12),
        (" 12.95 ", 12),
        ("-2.95", -2),
        ("", None),
        ("not-numeric", None),
        (None, None),
        ({"value": 1}, None),
        ([1], None),
    ],
)
def test_normalize_probe_readback_preserves_coercion(make_config, raw, expected):
    monitor = PecronMonitor(make_config())

    assert monitor._normalize_probe_readback(raw) == expected


def test_probe_missing_device_returns_exact_details_without_side_effects(make_config):
    monitor = make_probe_monitor(make_config)

    result = monitor.probe_control_values("missing-device", CONTROL_CODE, min_value=9, max_value=12)

    assert result == {
        "device_key": "missing-device",
        "control_code": CONTROL_CODE,
        "valid_values": [],
        "stop_value": 0,
        "last_readback": None,
        "reason": "device_not_found",
    }
    monitor.send_control.assert_not_called()
    monitor._request_status.assert_not_called()


def test_probe_missing_control_returns_exact_details_without_side_effects(make_config):
    monitor = make_probe_monitor(make_config)

    result = monitor.probe_control_values(DEVICE_KEY, "missing-control", min_value=9, max_value=12)

    assert result == {
        "device_key": DEVICE_KEY,
        "control_code": "missing-control",
        "valid_values": [],
        "stop_value": 0,
        "last_readback": None,
        "reason": "control_not_found",
    }
    monitor.send_control.assert_not_called()
    monitor._request_status.assert_not_called()


def test_probe_send_failure_stops_before_cache_clear_request_or_sleep(make_config):
    monitor = make_probe_monitor(make_config)
    cached = {CONTROL_CODE: 99}
    monitor.latest_data = {DEVICE_KEY: cached, "other-device": {"value": 8}}
    monitor.send_control.return_value = False

    with patch("monitor_controls.time.sleep") as sleep:
        result = monitor.probe_control_values(DEVICE_KEY, CONTROL_CODE, min_value=3, max_value=7)

    assert result == {
        "device_key": DEVICE_KEY,
        "control_code": CONTROL_CODE,
        "valid_values": [],
        "stop_value": 3,
        "last_readback": None,
        "reason": "send_failed",
    }
    monitor.send_control.assert_called_once_with(DEVICE_KEY, CONTROL_CODE, 3)
    monitor._request_status.assert_not_called()
    sleep.assert_not_called()
    assert monitor.latest_data == {
        DEVICE_KEY: cached,
        "other-device": {"value": 8},
    }


def test_probe_clears_only_target_cache_and_preserves_exact_operation_order(make_config):
    monitor = make_probe_monitor(make_config)
    monitor.latest_data = {
        DEVICE_KEY: {CONTROL_CODE: "stale"},
        "other-device": {"value": 8},
    }
    events = []
    readbacks = iter((" 2.75 ", "wrong"))

    def send(device_key, control_code, value):
        events.append(("send", device_key, control_code, value))
        return True

    def request():
        events.append(("request",))
        assert DEVICE_KEY not in monitor.latest_data
        assert monitor.latest_data["other-device"] == {"value": 8}
        raw = next(readbacks)
        monitor.latest_data[DEVICE_KEY] = {"wrapper": [{"deeper": {CONTROL_CODE: raw}}]}

    def sleep(seconds):
        events.append(("sleep", seconds))

    monitor.send_control.side_effect = send
    monitor._request_status.side_effect = request

    with patch("monitor_controls.time.sleep", side_effect=sleep):
        result = monitor.probe_control_values(DEVICE_KEY, CONTROL_CODE, min_value=2, max_value=5)

    assert events == [
        ("send", DEVICE_KEY, CONTROL_CODE, 2),
        ("sleep", 3),
        ("request",),
        ("sleep", 1),
        ("send", DEVICE_KEY, CONTROL_CODE, 3),
        ("sleep", 3),
        ("request",),
        ("sleep", 1),
    ]
    assert result == {
        "device_key": DEVICE_KEY,
        "control_code": CONTROL_CODE,
        "valid_values": [2],
        "stop_value": 3,
        "last_readback": "wrong",
        "reason": "readback_mismatch",
    }
    assert monitor.latest_data["other-device"] == {"value": 8}
    assert monitor.latest_data[DEVICE_KEY] == {"wrapper": [{"deeper": {CONTROL_CODE: "wrong"}}]}


def test_probe_includes_both_bounds_and_reports_max_reached(make_config):
    monitor = make_probe_monitor(make_config)
    readbacks = iter(("4.99", 5.75))

    def request():
        candidate_readback = next(readbacks)
        monitor.latest_data[DEVICE_KEY] = {CONTROL_CODE: candidate_readback}

    monitor.send_control.return_value = True
    monitor._request_status.side_effect = request

    with patch("monitor_controls.time.sleep") as sleep:
        result = monitor.probe_control_values(DEVICE_KEY, CONTROL_CODE, min_value=4, max_value=5)

    assert result == {
        "device_key": DEVICE_KEY,
        "control_code": CONTROL_CODE,
        "valid_values": [4, 5],
        "stop_value": 5,
        "last_readback": 5.75,
        "reason": "max_reached",
    }
    assert monitor.send_control.call_args_list == [
        call(DEVICE_KEY, CONTROL_CODE, 4),
        call(DEVICE_KEY, CONTROL_CODE, 5),
    ]
    assert monitor._request_status.call_args_list == [call(), call()]
    assert sleep.call_args_list == [call(3), call(1), call(3), call(1)]


def test_probe_empty_reversed_bounds_report_max_reached_without_attempt(make_config):
    monitor = make_probe_monitor(make_config)

    with patch("monitor_controls.time.sleep") as sleep:
        result = monitor.probe_control_values(DEVICE_KEY, CONTROL_CODE, min_value=6, max_value=5)

    assert result == {
        "device_key": DEVICE_KEY,
        "control_code": CONTROL_CODE,
        "valid_values": [],
        "stop_value": 6,
        "last_readback": None,
        "reason": "max_reached",
    }
    monitor.send_control.assert_not_called()
    monitor._request_status.assert_not_called()
    sleep.assert_not_called()
