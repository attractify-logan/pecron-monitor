#!/usr/bin/env python3
"""Tests for LocalTransport read-back verification (issue #46).

`LocalTransport.send_control` previously reported success regardless of whether
the device actually applied the write. After the fix it does a post-write
`read_status` and compares the data point against the requested value, so
silent failures (rejected writes, watchdog-resets) surface as `False`.
"""

import unittest
from unittest.mock import MagicMock

# sys.path is handled globally by tests/conftest.py
from local_transport import LocalTransport, _control_values_equal


def make_transport(controls=None):
    """Build a LocalTransport with auth/socket state stubbed for unit testing."""
    t = LocalTransport(
        device_ip="192.168.1.99",
        auth_key_b64="AAAAAAAAAAAAAAAAAAAAAA==",  # 16 zero bytes
        device_key="DEV1",
        controls=controls or {},
    )
    t._sock = MagicMock()
    t._iv = b"\x00" * 16
    t._encrypted = True
    t._connected = True
    return t


CONTROLS_BOOL = {
    "dc_switch_hm": {"id": 38, "type": "BOOL", "access": "RW"},
    "ac_switch_hm": {"id": 40, "type": "BOOL", "access": "RW"},
}

CONTROLS_ENUM = {
    "ac_charging_power_ios": {"id": 51, "type": "ENUM", "access": "RW"},
}


class TestSendControlReadback(unittest.TestCase):

    def test_returns_true_when_readback_confirms_bool_write(self):
        """Standard happy path: device's post-write state matches the request."""
        t = make_transport(CONTROLS_BOOL)
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock(return_value={"dc_switch_hm": False})

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertTrue(result)
        t.read_status.assert_called_once()

    def test_returns_false_when_readback_value_mismatches(self):
        """Device acknowledged the write but didn't actually flip the relay."""
        t = make_transport(CONTROLS_BOOL)
        t._send_control_packet = MagicMock(return_value=True)
        # We tried to set False; device still reports True.
        t.read_status = MagicMock(return_value={"dc_switch_hm": True})

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertFalse(result)

    def test_returns_false_when_readback_returns_empty(self):
        """Device returned no fields (possibly mid-watchdog-reset)."""
        t = make_transport(CONTROLS_BOOL)
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock(return_value={})

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertFalse(result)

    def test_returns_false_when_readback_missing_target_field(self):
        """Read came back populated but doesn't include the data point we wrote."""
        t = make_transport(CONTROLS_BOOL)
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock(return_value={"voltage": 52.4, "soc_percent": 73})

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertFalse(result)

    def test_returns_false_when_readback_raises(self):
        """TCP read-back failed (e.g. connection drop after device reset)."""
        t = make_transport(CONTROLS_BOOL)
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock(side_effect=ConnectionError("device reset mid-stream"))

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertFalse(result)

    def test_returns_true_with_warning_when_controls_empty(self):
        """No TSL means we can't index a read response; preserve pre-fix behavior
        rather than regress callers that don't pass controls."""
        t = make_transport(controls={})
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock()

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertTrue(result)
        t.read_status.assert_not_called()

    def test_returns_true_when_data_point_not_in_controls(self):
        """Controls populated but no entry matches data_point_id; same fallback."""
        t = make_transport(CONTROLS_BOOL)  # only ids 38/40
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock()

        result = t.send_control(data_point_id=99, value=False, ctrl_type="BOOL")

        self.assertTrue(result)
        t.read_status.assert_not_called()

    def test_returns_false_when_packet_send_fails(self):
        """Transport-level write failure short-circuits before read-back."""
        t = make_transport(CONTROLS_BOOL)
        t._send_control_packet = MagicMock(return_value=False)
        t.read_status = MagicMock()

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertFalse(result)
        t.read_status.assert_not_called()

    def test_enum_write_readback_int_compare(self):
        """ENUM/INT values normalize through int() on both sides."""
        t = make_transport(CONTROLS_ENUM)
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock(return_value={"ac_charging_power_ios": 2})

        result = t.send_control(data_point_id=51, value=2, ctrl_type="ENUM")

        self.assertTrue(result)

    def test_enum_write_readback_mismatch(self):
        t = make_transport(CONTROLS_ENUM)
        t._send_control_packet = MagicMock(return_value=True)
        t.read_status = MagicMock(return_value={"ac_charging_power_ios": 5})

        result = t.send_control(data_point_id=51, value=2, ctrl_type="ENUM")

        self.assertFalse(result)

    def test_disconnected_transport_short_circuits(self):
        t = make_transport(CONTROLS_BOOL)
        t._connected = False  # disconnected
        t._send_control_packet = MagicMock()
        t.read_status = MagicMock()

        result = t.send_control(data_point_id=38, value=False, ctrl_type="BOOL")

        self.assertFalse(result)
        t._send_control_packet.assert_not_called()
        t.read_status.assert_not_called()


class TestControlValuesEqual(unittest.TestCase):

    def test_bool_true_matches_true(self):
        self.assertTrue(_control_values_equal(True, True, "BOOL"))

    def test_bool_false_matches_false(self):
        self.assertTrue(_control_values_equal(False, False, "BOOL"))

    def test_bool_true_matches_int_one(self):
        """Pecron parser yields bool, but be defensive about 0/1 ints too."""
        self.assertTrue(_control_values_equal(True, 1, "BOOL"))
        self.assertTrue(_control_values_equal(1, True, "BOOL"))

    def test_bool_mismatch(self):
        self.assertFalse(_control_values_equal(True, False, "BOOL"))
        self.assertFalse(_control_values_equal(False, True, "BOOL"))

    def test_int_match(self):
        self.assertTrue(_control_values_equal(5, 5, "ENUM"))
        self.assertTrue(_control_values_equal(0, 0, "INT"))

    def test_int_mismatch(self):
        self.assertFalse(_control_values_equal(5, 4, "ENUM"))

    def test_unparseable_returns_false(self):
        self.assertFalse(_control_values_equal("not-a-number", 5, "INT"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
