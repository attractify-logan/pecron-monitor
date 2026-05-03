"""Tests for issue #60: suppress LOCAL TCP shutdown-window zero-frames.

When the inverter is gating off during a low-battery shutdown, local TCP
returns a frame with fresh battery_pct (0) and voltage but zeroed power and
remain_time. Those zeroes are technically real (the inverter has stopped
drawing) but they overwrite the cloud's last-known-good values in HA for
the 1-2 minute shutdown window, making "if input < 5W" automations false-fire.

Bruce's reproduction (issue #60 / #57 comments): on E3600LFP at battery=0%,
local TCP returned `In:0W Out:0W ⏱ 0h0m` while cloud simultaneously reported
`In:1281W Out:1198W ⏱ 89h48m`.

The fix: in `_process_data`, detect the shape (source in LOCAL TCP/BLE +
battery_pct=0 + total_in=0 + total_out=0 + remain<=0) and skip the status
log + HA publish for that single frame. Cloud MQTT continues to drive HA.
"""

import logging
from unittest.mock import MagicMock

import pytest

from monitor import PecronMonitor


def _make_monitor():
    """Construct a minimal PecronMonitor with the fields _process_data touches."""
    m = PecronMonitor.__new__(PecronMonitor)
    m.config = {"alerts": {}, "rules": []}
    m.devices = [{"device_key": "DK_TEST", "device_name": "TestDevice"}]
    m.latest_data = {}
    m.data_sources = {}
    m._last_logged_values = {}
    m.last_alert = {}
    m.last_rule_action = {}
    m.rules = []
    m.ha_bridge = MagicMock()
    m.ha_bridge.publish_state = MagicMock()
    return m


# Bruce's frame shape from issue #60: battery=0%, voltage=47.7V, all power=0,
# remain=0. Voltage and temperature are nested under host_packet_data_jdb because
# that's the path SENSOR_FIELDS["voltage"] resolves; battery_percentage is at
# top level.
SHUTDOWN_ZERO_FRAME = {
    "battery_percentage": 0,
    "host_packet_data_jdb": {
        "host_packet_voltage": 47.7,
        "host_packet_temp": 31,
        "host_packet_electric_percentage": 0,
    },
    "total_input_power": 0,
    "total_output_power": 0,
    "remain_time": 0,
}


class TestShutdownZeroFrameSuppression:
    def test_local_tcp_shutdown_zero_frame_skips_ha_publish(self, caplog):
        m = _make_monitor()
        caplog.set_level(logging.DEBUG, logger="pecron")
        m._process_data("DK_TEST", SHUTDOWN_ZERO_FRAME, source="LOCAL TCP")
        m.ha_bridge.publish_state.assert_not_called()
        assert any("shutdown-window zero-frame" in r.message for r in caplog.records)

    def test_ble_shutdown_zero_frame_also_suppressed(self):
        m = _make_monitor()
        m._process_data("DK_TEST", SHUTDOWN_ZERO_FRAME, source="BLE")
        m.ha_bridge.publish_state.assert_not_called()

    def test_cloud_mqtt_zero_frame_NOT_suppressed(self):
        # Same frame shape but source=CLOUD MQTT: cloud is the authoritative
        # source for power, so 0 from cloud must reach HA. (E.g. device truly
        # idle, cloud has caught up with the shutdown.)
        m = _make_monitor()
        m._process_data("DK_TEST", SHUTDOWN_ZERO_FRAME, source="CLOUD MQTT")
        m.ha_bridge.publish_state.assert_called_once()

    def test_local_tcp_with_real_power_NOT_suppressed(self):
        # Local TCP frame at battery=0% but with non-zero power (impossible in
        # practice but the suppression rule must require ALL power=0 to fire).
        m = _make_monitor()
        kv = dict(SHUTDOWN_ZERO_FRAME, total_input_power=100)
        m._process_data("DK_TEST", kv, source="LOCAL TCP")
        m.ha_bridge.publish_state.assert_called_once()

    def test_local_tcp_with_battery_above_zero_NOT_suppressed(self):
        # Same all-zero power but battery is 50%: that's a different shape
        # (idle device, healthy battery) and should reach HA. Need to override
        # the nested host_packet_electric_percentage path too because that's
        # what SENSOR_FIELDS["battery_percent"] reads first.
        m = _make_monitor()
        kv = dict(
            SHUTDOWN_ZERO_FRAME,
            battery_percentage=50,
            host_packet_data_jdb={
                **SHUTDOWN_ZERO_FRAME["host_packet_data_jdb"],
                "host_packet_electric_percentage": 50,
            },
        )
        m._process_data("DK_TEST", kv, source="LOCAL TCP")
        m.ha_bridge.publish_state.assert_called_once()

    def test_local_tcp_with_remain_above_zero_NOT_suppressed(self):
        # Power=0 but remain_time is positive: the inverter says it could run
        # for some time, so this isn't the shutdown placeholder.
        m = _make_monitor()
        kv = dict(SHUTDOWN_ZERO_FRAME, remain_time=120)
        m._process_data("DK_TEST", kv, source="LOCAL TCP")
        m.ha_bridge.publish_state.assert_called_once()

    def test_suppressed_frame_does_not_update_last_logged_values(self):
        # The "values unchanged" dedupe in _process_data must not be poisoned
        # by the suppressed shutdown frame.
        m = _make_monitor()
        m._process_data("DK_TEST", SHUTDOWN_ZERO_FRAME, source="LOCAL TCP")
        assert "DK_TEST" not in m._last_logged_values
