#!/usr/bin/env python3
"""Tests for cloud recovery after offline fallback (issue #23).

Verifies:
1. A transient cloud login failure sets the "fell back to offline" flag.
2. _try_cloud_recovery() is a no-op until cloud_retry_interval has elapsed.
3. On a successful retry, offline_mode clears and MQTT is reconnected.
4. A user-forced --offline run never triggers cloud recovery.
"""

import base64
import time
import unittest
from unittest.mock import MagicMock, patch

# sys.path + paho mocking are handled globally by tests/conftest.py
from monitor import PecronMonitor

FAKE_AUTH_KEY = base64.b64encode(b"0123456789abcdef").decode()


def make_config():
    return {
        "email": "test@test.com",
        "password": "test",
        "region": "na",
        "devices": [
            {
                "product_key": "p11u2b",
                "device_key": "682499E40D61",
                "name": "E1500LFP",
                "lan_ip": "192.168.68.51",
                "auth_key": FAKE_AUTH_KEY,
            }
        ],
        "poll_interval": 60,
        "cloud_retry_interval": 300,
    }


class TestCloudRecovery(unittest.TestCase):
    def test_transient_failure_sets_fallback_flag(self):
        """A DNS-style cloud login failure leaves the monitor in a retry-eligible state."""
        config = make_config()
        monitor = PecronMonitor(config)
        # Even though devices have lan_ip+auth_key (offline-capable), we expect the
        # fallback path to run because login() raises, not the forced-offline path.
        with patch("monitor.login", side_effect=OSError("Temporary failure in name resolution")):
            monitor.authenticate(force_offline=False)
        self.assertTrue(monitor.offline_mode, "Should fall back to offline on login failure")
        self.assertTrue(monitor._fell_back_to_offline, "Should flag the fallback as unplanned")
        self.assertGreater(
            monitor._last_cloud_retry_at,
            0,
            "Fallback should stamp _last_cloud_retry_at so the first retry waits",
        )

    def test_forced_offline_never_retries(self):
        """`--offline` runs are an explicit user choice and must not retry cloud."""
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.authenticate(force_offline=True)
        self.assertTrue(monitor.offline_mode)
        self.assertFalse(
            monitor._fell_back_to_offline,
            "Forced offline must not be flagged as a transient fallback",
        )
        # Even without the interval guard, recovery should bail out immediately.
        self.assertFalse(
            monitor._try_cloud_recovery(), "Forced offline mode should never attempt cloud recovery"
        )

    def test_recovery_respects_retry_interval(self):
        """Between retries the monitor stays silent; one attempt per interval only."""
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.offline_mode = True
        monitor._fell_back_to_offline = True
        monitor._last_cloud_retry_at = time.time()  # just attempted
        with patch("monitor.login") as mock_login:
            self.assertFalse(
                monitor._try_cloud_recovery(), "Should not retry inside the cooldown window"
            )
            mock_login.assert_not_called()

    def test_successful_recovery_exits_offline_mode(self):
        """On retry success the monitor rejoins cloud and clears the flags."""
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.offline_mode = True
        monitor._fell_back_to_offline = True
        monitor._last_cloud_retry_at = 0  # cooldown already passed
        monitor.skip_local_setup = True  # keep the test focused on cloud-side state
        monitor.connect_mqtt = MagicMock()

        fake_token = {"token": "t", "uid": "U1", "expires_at": time.time() + 3600}
        with (
            patch("monitor.login", return_value=fake_token),
            patch(
                "monitor.resolve_devices",
                return_value=[
                    {
                        "product_key": "p11u2b",
                        "device_key": "682499E40D61",
                        "device_name": "E1500LFP",
                        "product_name": "E1500LFP",
                        "controls": {},
                    }
                ],
            ),
        ):
            recovered = monitor._try_cloud_recovery()

        self.assertTrue(recovered, "Retry should report success")
        self.assertFalse(monitor.offline_mode, "offline_mode should clear on recovery")
        self.assertFalse(monitor._fell_back_to_offline, "fallback flag should clear on recovery")
        self.assertEqual(monitor.token_data, fake_token)
        monitor.connect_mqtt.assert_called_once()

    def test_failed_recovery_stays_offline_and_defers(self):
        """A continuing network failure leaves offline_mode intact and records the attempt."""
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.offline_mode = True
        monitor._fell_back_to_offline = True
        monitor._last_cloud_retry_at = 0

        with patch("monitor.login", side_effect=OSError("Temporary failure in name resolution")):
            recovered = monitor._try_cloud_recovery()

        self.assertFalse(recovered)
        self.assertTrue(monitor.offline_mode)
        self.assertTrue(monitor._fell_back_to_offline)
        self.assertGreater(
            monitor._last_cloud_retry_at,
            0,
            "Failed attempt still arms the cooldown so we don't hammer the cloud",
        )

    def test_token_refresh_remains_disabled_in_offline_mode(self):
        """Regression: offline-mode token refresh short-circuit must still hold."""
        config = make_config()
        monitor = PecronMonitor(config)
        monitor.offline_mode = True
        monitor._fell_back_to_offline = True
        monitor.token_data = None
        self.assertFalse(
            monitor._token_needs_refresh(),
            "Offline mode should never try to refresh the cloud token",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
