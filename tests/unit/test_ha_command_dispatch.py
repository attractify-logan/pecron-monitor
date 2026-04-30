#!/usr/bin/env python3
"""Tests for issue #54: PecronMonitor._ha_command must dispatch every slug
that ha_bridge._publish_discovery advertises with a command_topic.

Issue #49 (PR #51) wired up the subscribe side: every command_topic registered
in discovery is now subscribed. But the dispatch side (_ha_command in
monitor.py) still hardcoded {ac, dc, ups}, so commands for eco_mode,
touch_lock, and auto_light_flag_as (auto_dim) were silently dropped at the
last hop -- HA toggle UI looked live but nothing reached the device.

The fix extends ctrl_map to mirror every command_topic from discovery, plus
adds a defense-in-depth WARN log when an unknown control arrives so a future
discovery addition that misses this map fails loudly instead of silently.
"""

import base64
import logging
import unittest
from unittest.mock import MagicMock

# sys.path + paho mocking are handled globally by tests/conftest.py
from monitor import PecronMonitor

FAKE_AUTH = base64.b64encode(b"0123456789abcdef").decode()


def make_monitor():
    """Build a PecronMonitor with mocked send_bool_control.

    Mirrors the make_monitor pattern from test_model_behavior.py but mocks
    send_bool_control specifically (the method _ha_command actually calls)
    rather than send_control, so the test asserts on the dispatch path one
    layer up.
    """
    config = {
        "email": "x", "password": "x", "region": "na",
        "devices": [{"product_key": "pk", "device_key": "dk0", "name": "E3800",
                     "lan_ip": "10.0.0.1", "auth_key": FAKE_AUTH}],
    }
    m = PecronMonitor(config)
    m.devices = [{"product_key": "pk", "device_key": "dk0",
                  "device_name": "E3800", "product_name": "E3800",
                  "controls": {}}]
    m.send_bool_control = MagicMock()
    return m


# Slug -> TSL field, sourced from ha_bridge._publish_discovery command_topic
# registrations and confirmed against constants.E3800_FULL_TSL (all six are
# BOOL/RW). Keep this aligned with monitor._ha_command.ctrl_map.
SLUG_TSL_PAIRS = [
    ("ac", "ac_switch_hm"),
    ("dc", "dc_switch_hm"),
    ("ups", "ups_status_hm"),
    ("eco_mode", "eco_quite_mode_as"),
    ("touch_lock", "device_touch_locking_as"),
    ("auto_light_flag_as", "auto_light_flag_as"),
]


class TestHaCommandDispatch(unittest.TestCase):
    """Issue #54: every discovered slug must dispatch to the right TSL code."""

    def test_each_slug_dispatches_on(self):
        for slug, tsl_code in SLUG_TSL_PAIRS:
            with self.subTest(slug=slug):
                m = make_monitor()
                m._ha_command("dk0", slug, True)
                m.send_bool_control.assert_called_once_with("dk0", tsl_code, True)

    def test_each_slug_dispatches_off(self):
        for slug, tsl_code in SLUG_TSL_PAIRS:
            with self.subTest(slug=slug):
                m = make_monitor()
                m._ha_command("dk0", slug, False)
                m.send_bool_control.assert_called_once_with("dk0", tsl_code, False)

    def test_unknown_slug_is_dropped_and_warns(self):
        """Defense-in-depth: an unrecognized control must NOT call
        send_bool_control AND must emit a WARN log so the gap is visible
        in production rather than silent."""
        m = make_monitor()
        with self.assertLogs("pecron", level="WARNING") as cm:
            m._ha_command("dk0", "made_up_thing", True)
        m.send_bool_control.assert_not_called()
        # Match on the salient bits rather than the exact phrasing so
        # message tweaks don't break the test.
        joined = "\n".join(cm.output)
        self.assertIn("made_up_thing", joined)
        self.assertIn("dk0", joined)
        self.assertIn("#54", joined)

    def test_known_slug_does_not_warn(self):
        """The happy path must stay quiet -- WARN-on-unknown is only for the
        defense-in-depth case."""
        m = make_monitor()
        # assertNoLogs is 3.10+; use a manual handler capture to stay
        # compatible with whatever the project's minimum is.
        records = []
        handler = logging.Handler()
        handler.setLevel(logging.WARNING)
        handler.emit = records.append
        logger = logging.getLogger("pecron")
        logger.addHandler(handler)
        try:
            m._ha_command("dk0", "ac", True)
        finally:
            logger.removeHandler(handler)
        warn_records = [r for r in records if r.levelno >= logging.WARNING
                        and "#54" in r.getMessage()]
        self.assertEqual(warn_records, [],
                         f"unexpected WARN on known slug: {[r.getMessage() for r in warn_records]}")
        m.send_bool_control.assert_called_once_with("dk0", "ac_switch_hm", True)


class TestHaCommandMapMatchesDiscovery(unittest.TestCase):
    """Regression guard: the dispatch map must cover every command_topic that
    ha_bridge._publish_discovery advertises. If someone adds a switch to
    discovery without updating _ha_command, this test fails.
    """

    def test_every_discovery_command_topic_has_a_dispatch_entry(self):
        from ha_bridge import HomeAssistantBridge

        # E3800-shaped device with the TSL controls that gate the optional
        # switches (eco_mode, touch_lock). Mirror the pattern from
        # test_command_subscriptions._e3800_device.
        device = {
            "device_key": "DEV1",
            "device_name": "E3800",
            "controls": {
                "eco_quite_mode_as": {},
                "device_touch_locking_as": {},
                "battery_temp": {},
                "charging_plate_temp": {},
            },
        }
        b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=[device])
        b.client = MagicMock()
        b._connected = True
        b._publish_discovery()

        # Extract slug from each command_topic: format is pecron/<dk>/<slug>/set
        discovered_slugs = set()
        for topic in b._command_topics:
            parts = topic.split("/")
            self.assertEqual(len(parts), 4, f"unexpected command_topic format: {topic}")
            self.assertEqual(parts[0], "pecron")
            self.assertEqual(parts[3], "set")
            discovered_slugs.add(parts[2])

        # Every discovered slug must be present in _ha_command's ctrl_map.
        # We exercise this via the public surface: a known slug calls
        # send_bool_control, an unknown one does not.
        for slug in sorted(discovered_slugs):
            with self.subTest(slug=slug):
                m = make_monitor()
                m._ha_command("dk0", slug, True)
                m.send_bool_control.assert_called_once(),  # not silently dropped


if __name__ == "__main__":
    unittest.main(verbosity=2)
