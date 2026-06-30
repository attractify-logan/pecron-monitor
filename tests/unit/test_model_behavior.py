#!/usr/bin/env python3
"""Tests for per-model behavior flags (issue #14).

Verifies that `high_frequency_reporting` is skipped for models where it's a
known no-op (E3600LFP) and still sent for models where it's effective.
"""

import base64
import unittest
from unittest.mock import MagicMock, patch

# sys.path + paho mocking are handled globally by tests/conftest.py
from constants import MODEL_BEHAVIOR
from monitor import PecronMonitor

FAKE_AUTH = base64.b64encode(b"0123456789abcdef").decode()


def make_monitor(device_names):
    config = {
        "email": "x",
        "password": "x",
        "region": "na",
        "devices": [
            {
                "product_key": "pk",
                "device_key": f"dk{i}",
                "name": n,
                "lan_ip": f"10.0.0.{i + 1}",
                "auth_key": FAKE_AUTH,
            }
            for i, n in enumerate(device_names)
        ],
    }
    m = PecronMonitor(config)
    m.devices = [
        {
            "product_key": "pk",
            "device_key": f"dk{i}",
            "device_name": n,
            "product_name": n,
            "controls": {},
        }
        for i, n in enumerate(device_names)
    ]
    m.send_control = MagicMock()  # don't actually try to send anything
    return m


class TestModelBehavior(unittest.TestCase):
    def test_e3600_flag_set(self):
        self.assertFalse(MODEL_BEHAVIOR["E3600LFP"]["high_freq_effective"])
        self.assertFalse(MODEL_BEHAVIOR["E3600"]["high_freq_effective"])

    def test_enable_skips_e3600(self):
        m = make_monitor(["E3600LFP"])
        m._enable_high_freq_reporting()
        m.send_control.assert_not_called()

    def test_enable_sends_for_e3800(self):
        m = make_monitor(["E3800LFP"])
        m._enable_high_freq_reporting()
        # high_frequency_reporting is transient -- the device auto-reverts the
        # value, so the warm-up callers pass verify=False to skip the noisy
        # post-write read-back (issue #50).
        m.send_control.assert_called_once_with("dk0", "high_frequency_reporting", 3, verify=False)

    def test_mixed_fleet_sends_only_for_effective_models(self):
        """A mixed config should send for E3800LFP and skip E3600LFP."""
        m = make_monitor(["E3800LFP", "E3600LFP", "E1500LFP"])
        m._enable_high_freq_reporting()
        sent_dks = [call.args[0] for call in m.send_control.call_args_list]
        self.assertEqual(
            set(sent_dks), {"dk0", "dk2"}, f"Expected E3800LFP + E1500LFP only, got {sent_dks}"
        )

    def test_disable_skips_e3600(self):
        m = make_monitor(["E3600LFP"])
        m._disable_high_freq_reporting()
        m.send_control.assert_not_called()

    def test_disable_sends_for_e3800(self):
        m = make_monitor(["E3800LFP"])
        m._disable_high_freq_reporting()
        # See test_enable_sends_for_e3800 -- verify=False on transient writes (#50).
        m.send_control.assert_called_once_with("dk0", "high_frequency_reporting", 0, verify=False)

    def test_unknown_model_defaults_to_effective(self):
        """Baseline (unlisted models) keeps current behavior: send the setting."""
        m = make_monitor(["SomeNewModel"])
        m._enable_high_freq_reporting()
        # See test_enable_sends_for_e3800 -- verify=False on transient writes (#50).
        m.send_control.assert_called_once_with("dk0", "high_frequency_reporting", 3, verify=False)


class TestLocalReadTimeout(unittest.TestCase):
    """Regression for issue #84: E3600/E3800 local TCP multi-packet reads need
    a longer inter-packet timeout than the 3.0s global default, or the read
    cuts off before the packet carrying real voltage/temp telemetry arrives."""

    def test_e3800_gets_extended_timeout(self):
        m = make_monitor(["E3800LFP"])
        self.assertEqual(m._local_read_timeout(m.devices[0]), 5.0)

    def test_e3600_gets_extended_timeout(self):
        m = make_monitor(["E3600LFP"])
        self.assertEqual(m._local_read_timeout(m.devices[0]), 5.0)

    def test_unlisted_model_uses_default(self):
        m = make_monitor(["E1500LFP"])
        self.assertEqual(m._local_read_timeout(m.devices[0]), 3.0)

    def test_setup_passes_per_model_timeout_to_transport(self):
        """_setup_local_transports must thread the per-model timeout through
        to LocalTransport, not just compute it and drop it."""
        m = make_monitor(["E3800LFP"])
        with patch("monitor.HAS_LOCAL", True), patch("monitor.LocalTransport") as mock_transport:
            m._setup_local_transports()
            _, kwargs = mock_transport.call_args
            self.assertEqual(kwargs.get("multi_packet_timeout"), 5.0)


class TestE3600Capacity(unittest.TestCase):
    """Regression: v0.7.0 incorrectly set E3600LFP to 3600Wh. Should be 3072Wh."""

    def test_e3600_is_3072(self):
        from constants import BATTERY_CAPACITY_WH

        self.assertEqual(BATTERY_CAPACITY_WH["E3600LFP"], "3072")
        self.assertEqual(BATTERY_CAPACITY_WH["E3600"], "3072")


if __name__ == "__main__":
    unittest.main(verbosity=2)
