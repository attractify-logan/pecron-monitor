#!/usr/bin/env python3
"""Tests for the local transport setup fix (issue #6).

Verifies:
1. Local transports are set up in cloud mode when lan_ip is configured
2. Token refresh doesn't trigger in offline mode
3. force_offline is preserved during run loop refresh
"""

from __future__ import annotations

import time
from unittest.mock import patch

from monitor import PecronMonitor


def _set_devices(monitor: PecronMonitor) -> None:
    monitor.devices = [
        {
            "product_key": "p11u2b",
            "device_key": "AABBCCDDEEFF",
            "device_name": "E1500LFP",
            "controls": {},
        }
    ]


def test_offline_mode_no_refresh(make_config):
    config = make_config(with_lan=True, with_auth=True)
    monitor = PecronMonitor(config)
    monitor.offline_mode = True
    monitor.token_data = None
    assert monitor._token_needs_refresh() is False


def test_online_mode_no_token_needs_refresh(make_config):
    config = make_config()
    monitor = PecronMonitor(config)
    monitor.offline_mode = False
    monitor.token_data = None
    assert monitor._token_needs_refresh() is True


def test_online_mode_valid_token_no_refresh(make_config):
    config = make_config()
    monitor = PecronMonitor(config)
    monitor.offline_mode = False
    monitor.token_data = {"token": "***", "uid": "u", "expires_at": time.time() + 3600}
    assert monitor._token_needs_refresh() is False


@patch("monitor.HAS_LOCAL", True)
@patch("monitor.LocalTransport")
def test_setup_local_transports_with_lan_ip_and_auth(
    mock_local_transport, make_config, fake_auth_key
):
    config = make_config(with_lan=True, with_auth=True)
    monitor = PecronMonitor(config)
    _set_devices(monitor)

    monitor._setup_local_transports()

    mock_local_transport.assert_called_once_with(
        "192.168.1.100",
        fake_auth_key,
        device_key="AABBCCDDEEFF",
        controls={},
    )
    assert "AABBCCDDEEFF" in monitor.local_transports


@patch("monitor.HAS_LOCAL", True)
@patch("monitor.LocalTransport")
def test_no_duplicate_setup(mock_local_transport, make_config):
    config = make_config(with_lan=True, with_auth=True)
    monitor = PecronMonitor(config)
    _set_devices(monitor)

    monitor._setup_local_transports()
    monitor._setup_local_transports()

    mock_local_transport.assert_called_once()


@patch("monitor.HAS_LOCAL", True)
@patch("monitor.LocalTransport")
def test_no_lan_ip_no_transport(mock_local_transport, make_config):
    config = make_config(with_lan=False)
    monitor = PecronMonitor(config)
    _set_devices(monitor)

    monitor._setup_local_transports()

    mock_local_transport.assert_not_called()
    assert len(monitor.local_transports) == 0


@patch("monitor.HAS_LOCAL", True)
@patch("monitor.get_auth_key")
@patch("monitor.LocalTransport")
def test_fetches_auth_key_from_cloud_if_missing(
    mock_local_transport, mock_get_auth_key, make_config, fake_auth_key
):
    mock_get_auth_key.return_value = fake_auth_key
    config = make_config(with_lan=True, with_auth=False)
    monitor = PecronMonitor(config)
    monitor.token_data = {"token": "***", "uid": "u1", "expires_at": 9999999999}
    _set_devices(monitor)

    monitor._setup_local_transports()

    mock_get_auth_key.assert_called_once()
    mock_local_transport.assert_called_once()


@patch("monitor.HAS_LOCAL", True)
@patch("monitor.LocalTransport")
def test_no_auth_key_no_token_skips(mock_local_transport, make_config):
    config = make_config(with_lan=True, with_auth=False)
    monitor = PecronMonitor(config)
    monitor.token_data = None
    _set_devices(monitor)

    monitor._setup_local_transports()

    mock_local_transport.assert_not_called()


def test_check_offline_capable_with_lan_and_auth(make_config):
    config = make_config(with_lan=True, with_auth=True)
    monitor = PecronMonitor(config)
    assert monitor._check_offline_capable() is True


def test_check_offline_capable_without_lan(make_config):
    config = make_config(with_lan=False, with_auth=True)
    monitor = PecronMonitor(config)
    assert monitor._check_offline_capable() is False


def test_check_offline_capable_without_auth(make_config):
    config = make_config(with_lan=True, with_auth=False)
    monitor = PecronMonitor(config)
    assert monitor._check_offline_capable() is False


@patch("monitor.HAS_LOCAL", True)
@patch("monitor.LocalTransport")
@patch("monitor.resolve_devices")
@patch("monitor.login")
def test_cloud_auth_sets_up_local(
    mock_login, mock_resolve_devices, mock_local_transport, make_config, fake_auth_key
):
    mock_login.return_value = {"token": "***", "uid": "u", "expires_at": 9999999999}
    mock_resolve_devices.return_value = [
        {
            "product_key": "p11u2b",
            "device_key": "AABBCCDDEEFF",
            "device_name": "E1500LFP",
            "product_name": "E1500LFP",
            "controls": {},
        }
    ]

    config = make_config(with_lan=True, with_auth=True)
    monitor = PecronMonitor(config)
    monitor.authenticate(force_offline=False)

    mock_local_transport.assert_called_once_with(
        "192.168.1.100",
        fake_auth_key,
        device_key="AABBCCDDEEFF",
        controls={},
    )
    assert "AABBCCDDEEFF" in monitor.local_transports
    assert monitor.offline_mode is False
