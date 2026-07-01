#!/usr/bin/env python3
"""Regression for issue #84: status_once()'s active collection loop used to
re-request telemetry ONLY via MQTT cloud publish. In --local/offline mode
there is no mqtt_client, so that retry was a no-op -- the 45s "Collecting
data..." wait never re-attempted a local TCP read and just idled out on
whatever the single initial _request_status() call happened to catch.

Fix: the retry loop now calls _request_status() itself, which already
handles local TCP (unconditionally) and MQTT-publish (only if connected),
so offline mode gets real retries too.
"""

from unittest.mock import MagicMock, patch

from monitor import PecronMonitor


def make_offline_monitor(make_config):
    config = make_config(with_lan=True, with_auth=True)
    monitor = PecronMonitor(config)
    monitor.devices = [
        {
            "product_key": "p11u2b",
            "device_key": "AABBCCDDEEFF",
            "device_name": "E3800LFP",
            "controls": {},
        }
    ]
    monitor.mqtt_client = None  # offline/--local mode: no cloud connection
    monitor.latest_data = {}  # never resolves to complete telemetry
    return monitor


def test_offline_status_once_retries_via_request_status(make_config):
    monitor = make_offline_monitor(make_config)
    monitor.authenticate = MagicMock()
    monitor.offline_mode = True
    monitor._request_status = MagicMock()

    with patch("monitor.time.sleep"):
        monitor.status_once(force_offline=True)

    # max_wait=45s / check_interval=5s, re-request gated to every 10s ->
    # multiple retries during the wait, not just the single initial call.
    assert monitor._request_status.call_count > 1


def test_offline_status_once_stops_once_telemetry_arrives(make_config):
    monitor = make_offline_monitor(make_config)
    monitor.authenticate = MagicMock()
    monitor.offline_mode = True

    calls = {"n": 0}

    def fake_request_status():
        calls["n"] += 1
        if calls["n"] >= 2:
            monitor.latest_data["AABBCCDDEEFF"] = {
                "host_packet_data_jdb": {"host_packet_voltage": 53.4, "host_packet_temp": 35}
            }

    monitor._request_status = MagicMock(side_effect=fake_request_status)

    with patch("monitor.time.sleep"):
        monitor.status_once(force_offline=True)

    # Loop should exit as soon as telemetry lands, not run the full 45s.
    assert calls["n"] <= 3
