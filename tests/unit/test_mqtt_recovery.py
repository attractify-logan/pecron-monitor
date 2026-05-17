"""Tests for MQTT CONNACK failure recovery (#69)."""

import time
from unittest.mock import MagicMock

from monitor import PecronMonitor, mqtt


def test_on_connect_failure_tracks_rebuild_need(make_config):
    mqtt.CONNACK_ACCEPTED = 0
    mqtt.connack_string.return_value = "Server unavailable"
    monitor = PecronMonitor(make_config())

    monitor._on_connect(MagicMock(), None, None, 3)

    assert monitor._mqtt_connect_failures == 1


def test_on_connect_success_clears_failures(make_config):
    mqtt.CONNACK_ACCEPTED = 0
    monitor = PecronMonitor(make_config())
    monitor._mqtt_connect_failures = 2
    monitor.devices = []

    monitor._on_connect(MagicMock(), None, None, 0)

    assert monitor._mqtt_connect_failures == 0


def test_recover_mqtt_connection_rebuilds_client_after_cooldown(make_config):
    monitor = PecronMonitor(make_config())
    monitor.token_data = {"uid": "uid", "token": "token"}
    monitor.mqtt_client = MagicMock()
    monitor._mqtt_connect_failures = 1
    monitor._last_mqtt_rebuild_at = time.time() - 120
    monitor.connect_mqtt = MagicMock()

    assert monitor._recover_mqtt_connection() is True

    monitor.mqtt_client.loop_stop.assert_called_once()
    monitor.mqtt_client.disconnect.assert_called_once()
    monitor.connect_mqtt.assert_called_once()
    assert monitor._mqtt_connect_failures == 0


def test_recover_mqtt_connection_obeys_cooldown(make_config):
    monitor = PecronMonitor(make_config())
    monitor.mqtt_client = MagicMock()
    monitor._mqtt_connect_failures = 1
    monitor._last_mqtt_rebuild_at = time.time()
    monitor.connect_mqtt = MagicMock()

    assert monitor._recover_mqtt_connection() is False

    monitor.connect_mqtt.assert_not_called()
    assert monitor._mqtt_connect_failures == 1
