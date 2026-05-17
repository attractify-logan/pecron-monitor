"""
Shared pytest fixtures and test configuration for pecron-monitor.

Makes the project root importable from tests/ and mocks out paho.mqtt globally
so individual test files don't have to.
"""

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the project root importable (so `import protocol`, `import helpers`, etc. work)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Mock paho.mqtt before any test imports a module that depends on it.
# This keeps unit tests hermetic — no network, no real MQTT client.
sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MQTT_PAYLOADS_DIR = FIXTURES_DIR / "mqtt_payloads"
TTLV_PACKETS_DIR = FIXTURES_DIR / "ttlv_packets"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def mqtt_payloads_dir() -> Path:
    return MQTT_PAYLOADS_DIR


@pytest.fixture(scope="session")
def ttlv_packets_dir() -> Path:
    return TTLV_PACKETS_DIR


# ---------------------------------------------------------------------------
# Common test data
# ---------------------------------------------------------------------------

# A valid base64-encoded 16-byte AES key. Safe to use in tests.
FAKE_AUTH_KEY = base64.b64encode(b"0123456789abcdef").decode()


@pytest.fixture
def fake_auth_key() -> str:
    return FAKE_AUTH_KEY


@pytest.fixture
def make_config():
    """Factory fixture that builds a test config dict.

    Usage:
        def test_something(make_config):
            config = make_config(with_lan=True, with_auth=True)
    """

    def _make(with_lan: bool = False, with_auth: bool = False):
        device = {
            "product_key": "p11u2b",
            "device_key": "AABBCCDDEEFF",
            "name": "E1500LFP",
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

    return _make


@pytest.fixture
def sample_e1500_telemetry():
    """Synthetic telemetry payload matching the E1500LFP shape (host_packet_data_jdb nested).

    NOTE: Synthetic data. Replace with a real capture to tighten the contract test.
    """
    return {
        "host_packet_data_jdb": {
            "host_packet_electric_percentage": 73,
            "host_packet_voltage": 51.8,
            "host_packet_temp": 24,
            "host_packet_status": 2,
            "host_packet_current": 0.0,
            "host_packet_ac_switch": 0,
            "host_packet_dc_switch": 1,
            "host_packet_ups_status": 0,
        },
        "total_input_power": 145,
        "total_output_power": 0,
        "remain_time": 522,
        "ac_data_output_hm": {"ac_output_power": 0, "ac_output_voltage": 120},
        "dc_data_output_hm": {"dc_output_power": 0},
    }


@pytest.fixture
def sample_e300_telemetry():
    """Synthetic telemetry payload matching the E300LFP shape (top-level fields).

    NOTE: Synthetic data. Replace with a real capture to tighten the contract test.
    """
    return {
        "battery_percentage": 88,
        "battery_temp": 22,
        "total_input_power": 0,
        "total_output_power": 35,
        "remain_time": 240,
        "ac_switch_hm": 1,
        "dc_switch_hm": 0,
        "ups_status_hm": 0,
    }
