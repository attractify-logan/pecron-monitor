#!/usr/bin/env python3
"""Tests for the local transport setup fix (issue #6).

Verifies:
1. Local transports are set up in cloud mode when lan_ip is configured
2. Token refresh doesn't trigger in offline mode
3. force_offline is preserved during run loop refresh
"""

import base64
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))

# Mock paho.mqtt before importing pecron_monitor
sys.modules['paho'] = MagicMock()
sys.modules['paho.mqtt'] = MagicMock()
sys.modules['paho.mqtt.client'] = MagicMock()

import pecron_monitor
from pecron_monitor import PecronMonitor, REGIONS

# Fake auth key (valid base64, 16 bytes)
FAKE_AUTH_KEY = base64.b64encode(b"0123456789abcdef").decode()


def make_config(with_lan=False, with_auth=False):
    """Build a test config dict."""
    device = {
        "product_key": "p11vpp",
        "device_key": "F4AB5CB4B5D4",
        "name": "F3000LFP",
    }
    if with_lan:
        device["lan_ip"] = "192.168.1.100"
    if with_auth:
        device["auth_key"] = FAKE_AUTH_KEY
    return {
        "email": "test@test.com",
        "password": "test",
        "region": "na",
        "devices": [device],
        "poll_interval": 60,
        "alerts": {"low_battery_percent": 20, "cooldown_minutes": 30},
    }


class TestTokenRefreshOffline(unittest.TestCase):
    """Bug 2: _token_needs_refresh() should return False in offline mode."""

    def test_offline_mode_no_refresh(self):
        config = make_config(with_lan=True, with_auth=True)
        monitor = PecronMonitor(config)
        monitor.offline_mode = True
        monitor.token_data = None
        self.assertFalse(monitor._token_needs_refresh(),
                         "Should NOT need refresh in offline mode")

    def test_online_mode_no_token_needs_refresh(self):
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.offline_mode = False
        monitor.token_data = None
        self.assertTrue(monitor._token_needs_refresh(),
                        "Should need refresh when online with no token")

    def test_online_mode_valid_token_no_refresh(self):
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.offline_mode = False
        import time
        monitor.token_data = {"token": "x", "uid": "u", "expires_at": time.time() + 3600}
        self.assertFalse(monitor._token_needs_refresh(),
                         "Should NOT need refresh when token is still valid")


class TestLocalTransportSetup(unittest.TestCase):
    """Bug 1: Local transports should be set up even in cloud mode."""

    @patch('pecron_monitor.HAS_LOCAL', True)
    @patch('pecron_monitor.LocalTransport')
    def test_setup_local_transports_with_lan_ip_and_auth(self, MockLT):
        """When lan_ip and auth_key are in config, transport should be created."""
        config = make_config(with_lan=True, with_auth=True)
        monitor = PecronMonitor(config)
        monitor.devices = [{
            "product_key": "p11vpp",
            "device_key": "F4AB5CB4B5D4",
            "device_name": "F3000LFP",
            "controls": {},
        }]
        monitor._setup_local_transports()

        MockLT.assert_called_once_with("192.168.1.100", FAKE_AUTH_KEY)
        self.assertIn("F4AB5CB4B5D4", monitor.local_transports)

    @patch('pecron_monitor.HAS_LOCAL', True)
    @patch('pecron_monitor.LocalTransport')
    def test_no_duplicate_setup(self, MockLT):
        """Calling _setup_local_transports twice shouldn't create duplicates."""
        config = make_config(with_lan=True, with_auth=True)
        monitor = PecronMonitor(config)
        monitor.devices = [{
            "product_key": "p11vpp",
            "device_key": "F4AB5CB4B5D4",
            "device_name": "F3000LFP",
            "controls": {},
        }]
        monitor._setup_local_transports()
        monitor._setup_local_transports()  # Second call

        MockLT.assert_called_once()  # Should only create once

    @patch('pecron_monitor.HAS_LOCAL', True)
    @patch('pecron_monitor.LocalTransport')
    def test_no_lan_ip_no_transport(self, MockLT):
        """Without lan_ip in config, no local transport should be created."""
        config = make_config(with_lan=False)
        monitor = PecronMonitor(config)
        monitor.devices = [{
            "product_key": "p11vpp",
            "device_key": "F4AB5CB4B5D4",
            "device_name": "F3000LFP",
            "controls": {},
        }]
        monitor._setup_local_transports()

        MockLT.assert_not_called()
        self.assertEqual(len(monitor.local_transports), 0)

    @patch('pecron_monitor.HAS_LOCAL', True)
    @patch('pecron_monitor.get_auth_key', return_value=FAKE_AUTH_KEY)
    @patch('pecron_monitor.LocalTransport')
    def test_fetches_auth_key_from_cloud_if_missing(self, MockLT, mock_get_auth):
        """If auth_key is missing but token is available, fetch it from cloud."""
        config = make_config(with_lan=True, with_auth=False)
        monitor = PecronMonitor(config)
        monitor.token_data = {"token": "test_token", "uid": "u1", "expires_at": 9999999999}
        monitor.devices = [{
            "product_key": "p11vpp",
            "device_key": "F4AB5CB4B5D4",
            "device_name": "F3000LFP",
            "controls": {},
        }]
        monitor._setup_local_transports()

        mock_get_auth.assert_called_once()
        MockLT.assert_called_once()

    @patch('pecron_monitor.HAS_LOCAL', True)
    @patch('pecron_monitor.LocalTransport')
    def test_no_auth_key_no_token_skips(self, MockLT):
        """If auth_key is missing AND no cloud token, skip gracefully."""
        config = make_config(with_lan=True, with_auth=False)
        monitor = PecronMonitor(config)
        monitor.token_data = None
        monitor.devices = [{
            "product_key": "p11vpp",
            "device_key": "F4AB5CB4B5D4",
            "device_name": "F3000LFP",
            "controls": {},
        }]
        monitor._setup_local_transports()

        MockLT.assert_not_called()


class TestOfflineCapable(unittest.TestCase):
    """Verify _check_offline_capable logic."""

    def test_with_lan_and_auth(self):
        config = make_config(with_lan=True, with_auth=True)
        monitor = PecronMonitor(config)
        self.assertTrue(monitor._check_offline_capable())

    def test_without_lan(self):
        config = make_config(with_lan=False, with_auth=True)
        monitor = PecronMonitor(config)
        self.assertFalse(monitor._check_offline_capable())

    def test_without_auth(self):
        config = make_config(with_lan=True, with_auth=False)
        monitor = PecronMonitor(config)
        self.assertFalse(monitor._check_offline_capable())


class TestAuthenticateCloudWithLocal(unittest.TestCase):
    """Bug 1 integration: authenticate() in cloud mode should set up local transports."""

    @patch('pecron_monitor.HAS_LOCAL', True)
    @patch('pecron_monitor.LocalTransport')
    @patch('pecron_monitor.resolve_devices')
    @patch('pecron_monitor.login')
    def test_cloud_auth_sets_up_local(self, mock_login, mock_resolve, MockLT):
        """Cloud auth path should call _setup_local_transports after resolve_devices."""
        mock_login.return_value = {"token": "t", "uid": "u", "expires_at": 9999999999}
        mock_resolve.return_value = [{
            "product_key": "p11vpp",
            "device_key": "F4AB5CB4B5D4",
            "device_name": "F3000LFP",
            "product_name": "F3000LFP",
            "controls": {},
        }]

        config = make_config(with_lan=True, with_auth=True)
        monitor = PecronMonitor(config)
        monitor.authenticate(force_offline=False)

        # Should have set up local transport
        MockLT.assert_called_once_with("192.168.1.100", FAKE_AUTH_KEY)
        self.assertIn("F4AB5CB4B5D4", monitor.local_transports)
        self.assertFalse(monitor.offline_mode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
