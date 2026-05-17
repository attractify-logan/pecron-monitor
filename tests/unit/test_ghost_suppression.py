#!/usr/bin/env python3
"""Tests for ghost-pack / idle-port / SOC-fallback suppression.

Covers the three behavior changes that stop ha_bridge.publish_state from
populating cache keys that would render as '0' ghost entities in HA:

1. Per-pack sensors (pack_N_*) skipped when the slot reports status=4
   ('No Connection', i.e. no expansion pack in that bay).
2. Per-port DC-input sensors (dc5521, gx16mf1, gx16mf2) skipped when the
   port reports voltage=0 AND current=0 AND power=0 (nothing plugged in
   to that port, or the device model doesn't have the port).
3. soc_percent falls back to host_percent when the device only emits host-
   shape packets (E1500LFP), so HA's Battery (SOC) entity reflects the
   actual state instead of Unknown.
"""

import unittest
from unittest.mock import MagicMock

# sys.path + paho mocking are handled globally by tests/conftest.py
from ha_bridge import HomeAssistantBridge


def make_bridge():
    b = HomeAssistantBridge({"discovery_prefix": "homeassistant"}, devices=[])
    b.client = MagicMock()
    b._connected = True
    b._published_topics = set()
    return b


# -------------------- Ghost pack suppression --------------------


class TestPackSuppression(unittest.TestCase):
    def _pack(self, status, battery=0, voltage=0.0, current=0.0, temp=0):
        return {
            "charging_pack_status": status,
            "charging_pack_battery": battery,
            "charging_pack_voltage": voltage,
            "charging_pack_current": current,
            "charging_pack_temp": temp,
        }

    def test_disconnected_pack_produces_null_cache_keys(self):
        """Status 4 = No Connection. All pack_1_* keys present but None so HA
        sees JSON null and transitions to Unknown (omitting the keys would
        leave HA holding last-known value)."""
        b = make_bridge()
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 98,
            },
            "charging_pack_data_jdb": [
                self._pack(status=3, battery=95, voltage=53.0, current=0.5, temp=30),  # connected
                self._pack(status=4),  # No Connection
                self._pack(status=4),
                self._pack(status=4),
            ],
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        # Connected slot (0): all five keys present with real values
        for k in [
            "pack_0_status",
            "pack_0_battery",
            "pack_0_voltage",
            "pack_0_current",
            "pack_0_temp",
        ]:
            self.assertIn(k, cache, f"connected pack must populate {k}")
            self.assertIsNotNone(cache[k], f"connected pack {k} must have a value")
        # Disconnected slots: keys present but None
        for i in [1, 2, 3]:
            for suffix in ["status", "battery", "voltage", "current", "temp"]:
                key = f"pack_{i}_{suffix}"
                self.assertIn(key, cache, f"disconnected pack_{i} must publish {key} (as null)")
                self.assertIsNone(
                    cache[key], f"disconnected pack_{i} {key} must be None, got {cache[key]!r}"
                )

    def test_previously_cached_pack_nulled_when_disconnected(self):
        """If a pack WAS connected and is now status=4, overwrite stale values
        with None so HA's state JSON carries explicit nulls."""
        b = make_bridge()
        # Seed cache with a previously connected pack 1
        b._state_cache["DEV1"] = {
            "pack_1_battery": 80,
            "pack_1_voltage": 52.0,
            "pack_1_current": 0.3,
            "pack_1_temp": 28,
            "pack_1_status": "Charging",
        }
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 98,
            },
            "charging_pack_data_jdb": [self._pack(status=4)] * 4,
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        for suffix in ["status", "battery", "voltage", "current", "temp"]:
            key = f"pack_1_{suffix}"
            self.assertIsNone(
                cache[key],
                f"stale pack_1_{suffix} must be set to None after disconnect, got {cache[key]!r}",
            )


# -------------------- Idle port suppression --------------------


class TestPortSuppression(unittest.TestCase):
    def _kv(self, **port_values):
        """Build a kv that places per-port values under dc_data_input_hm,
        matching SENSOR_FIELDS paths."""
        return {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 98,
            },
            "dc_data_input_hm": port_values,
        }

    def test_port_discovery_deferred_until_first_observation(self):
        """Models without per-port breakdown (E1500LFP) never report any
        per-port field, so their discovery topics are never published and
        HA doesn't accumulate ghost Unknown entities."""
        b = make_bridge()
        b._device_dev_info["DEV1"] = {"identifiers": ["pecron_DEV1"]}
        kv_no_ports = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 97,
            },
            # No dc_data_input_hm: device doesn't emit per-port data.
        }
        b.publish_state("DEV1", kv_no_ports)
        self.assertEqual(
            b._deferred_ports_published,
            set(),
            "no discovery should fire when the device reports no port data",
        )
        # Confirm no per-port config topic was published either.
        published_ports = [
            call
            for call in b.client.publish.call_args_list
            if "dc5521" in call.args[0] or "gx16mf" in call.args[0]
        ]
        self.assertEqual(
            published_ports, [], "no discovery config topics for per-port entities expected"
        )

    def test_port_discovery_fires_on_first_observation(self):
        """A solar-capable device reporting gx16mf2 data publishes discovery
        for those 3 entities on the first packet. Idempotent on subsequent
        packets."""
        b = make_bridge()
        b._device_dev_info["DEV1"] = {"identifiers": ["pecron_DEV1"]}
        kv = self._kv(
            gx16mf2_input_voltage=44.0,
            gx16mf2_input_current=0.9,
            gx16mf2_input_power=39,
        )
        b.publish_state("DEV1", kv)
        self.assertIn(("DEV1", "gx16mf2"), b._deferred_ports_published)

        # 3 discovery topics published for gx16mf2.
        gx_topics = [
            c.args[0]
            for c in b.client.publish.call_args_list
            if "gx16mf2" in c.args[0] and c.args[0].endswith("/config")
        ]
        self.assertEqual(len(gx_topics), 3)

        # Second packet with the same port data: no additional discovery
        # publishes.
        pre = len(gx_topics)
        b.publish_state("DEV1", kv)
        gx_topics_after = [
            c.args[0]
            for c in b.client.publish.call_args_list
            if "gx16mf2" in c.args[0] and c.args[0].endswith("/config")
        ]
        self.assertEqual(
            len(gx_topics_after),
            pre,
            "discovery must be idempotent; no re-publish on second observation",
        )

    def test_idle_port_shows_honest_zeros(self):
        """For idle ports, 0V / 0A / 0W is the real reading (empty-port
        measurement). Publish the zeros; don't suppress them. Unknown on an
        empty port is worse UX than 0 because the device genuinely measures 0
        there, unlike the pack case where disconnected slots bleed misleading
        nonzero data."""
        b = make_bridge()
        kv = self._kv(
            dc5521_input_voltage=0,
            dc5521_input_current=0,
            dc5521_input_power=0,
            gx16mf1_input_voltage=0,
            gx16mf1_input_current=0,
            gx16mf1_input_power=0,
            gx16mf2_input_voltage=18.2,
            gx16mf2_input_current=2.1,
            gx16mf2_input_power=38,
        )
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        # Idle ports: present with zero values (NOT None, NOT absent)
        for port in ["dc5521", "gx16mf1"]:
            for suffix in ["voltage", "current", "power"]:
                key = f"{port}_input_{suffix}"
                self.assertIn(key, cache)
                self.assertEqual(
                    cache[key], 0, f"idle port {port} {key} must publish 0, got {cache[key]!r}"
                )
        # Active port: real values
        self.assertEqual(cache["gx16mf2_input_voltage"], 18.2)
        self.assertEqual(cache["gx16mf2_input_current"], 2.1)
        self.assertEqual(cache["gx16mf2_input_power"], 38)


# -------------------- SOC fallback --------------------


class TestSocFallback(unittest.TestCase):
    def test_soc_falls_back_to_host_percent(self):
        """When only host-shape packets arrive, soc_percent mirrors host_percent."""
        b = make_bridge()
        kv = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 98,
            },
            # No battery_percentage at top level -> soc_percent would stay None.
        }
        b.publish_state("DEV1", kv)
        cache = b._state_cache["DEV1"]
        self.assertEqual(cache.get("host_percent"), 98)
        self.assertEqual(
            cache.get("soc_percent"),
            98,
            "soc_percent must mirror host_percent when not independently set",
        )

    def test_explicit_soc_not_overwritten_by_host(self):
        """Devices WITH expansion packs: overall SOC and host % can legitimately
        differ; the explicit overall-shape reading must not be clobbered."""
        b = make_bridge()
        connected_pack = {
            "charging_pack_status": 3,  # balanced charging
            "charging_pack_battery": 80,
            "charging_pack_voltage": 53.0,
            "charging_pack_current": 1.2,
            "charging_pack_temp": 28,
        }

        # First packet: overall shape sets soc_percent. Pack data marks this as
        # a non-standalone unit so the fallback preserves the explicit reading.
        kv_overall = {
            "battery_percentage": 85,
            "charging_pack_data_jdb": [
                connected_pack,
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
            ],
        }
        b.publish_state("DEV1", kv_overall)
        self.assertEqual(b._state_cache["DEV1"].get("soc_percent"), 85)

        # Second packet: host shape with a different host_percent. soc_percent
        # is already populated and a pack is occupied, so fallback must NOT
        # rewrite it to host_percent.
        kv_host = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 90,
            },
            "charging_pack_data_jdb": [
                connected_pack,
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
            ],
        }
        b.publish_state("DEV1", kv_host)
        cache = b._state_cache["DEV1"]
        self.assertEqual(cache.get("host_percent"), 90)
        self.assertEqual(
            cache.get("soc_percent"),
            85,
            "explicit soc_percent must not be clobbered by host fallback "
            "on devices with expansion packs",
        )

    def test_standalone_pps_soc_refreshes_on_live_host_packet(self):
        """Issue #43: standalone PPS (no expansion packs) must keep soc_percent
        in sync with host_percent on every live host-shape packet, even after
        a stale overall-shape packet has populated soc_percent.

        Without this, a single overall packet (e.g. battery_percentage=100
        reported as the device entered shutdown) freezes soc_percent forever
        while host_percent continues updating from live cloud-MQTT host packets,
        breaking any HA automation that watches the SOC sensor."""
        b = make_bridge()

        # Step 1: a stale overall-shape packet seeds cache with soc_percent=100
        # (e.g. last value before the device went to shutdown). No pack data,
        # so the device is treated as standalone.
        kv_stale_overall = {"battery_percentage": 100}
        b.publish_state("DEV1", kv_stale_overall)
        self.assertEqual(
            b._state_cache["DEV1"].get("soc_percent"),
            100,
            "initial overall packet should populate soc_percent",
        )

        # Step 2: live host-shape packet arrives showing the device is
        # actually at 82%. soc_percent must follow because the device has
        # no expansion packs.
        kv_live_host = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 52.5,
                "host_packet_electric_percentage": 82,
            },
        }
        b.publish_state("DEV1", kv_live_host)
        cache = b._state_cache["DEV1"]
        self.assertEqual(cache.get("host_percent"), 82)
        self.assertEqual(
            cache.get("soc_percent"),
            82,
            "standalone PPS soc_percent must follow host_percent "
            "on live host packets even after a prior overall packet",
        )


# -------------------- Total power fallback (issue #48) --------------------


class TestTotalPowerFallback(unittest.TestCase):
    """Issue #48: same one-time-fill pattern as #43 but for total_input_power /
    total_output_power. The host-zero guard on the top-level total fields keeps
    a stale non-zero cached value when AC drops; the aggregate fallback never
    re-runs because the cached value is no longer None. Standalone PPS must
    re-aggregate from components on every packet so the published JSON tracks
    the live state. Devices with occupied packs preserve the original
    "fill once, don't clobber" behavior."""

    def test_standalone_pps_total_input_power_refreshes(self):
        """Stale cached total_input_power must drop to 0 on a standalone PPS
        when ac_input_power and dc_input_power both read 0 in a live packet."""
        b = make_bridge()

        # Step 1: live host packet with ac/dc input populated. The aggregate
        # fallback fills total_input_power=1500 (= 1200 + 300). Per-source
        # paths are nested under ac_data_input_hm / dc_data_input_hm to match
        # the live MQTT shape (see constants.SENSOR_FIELDS).
        kv_initial = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 95,
            },
            "ac_data_input_hm": {"ac_input_power": 1200},
            "dc_data_input_hm": {"dc_input_power": 300},
        }
        b.publish_state("DEV1", kv_initial)
        cache = b._state_cache["DEV1"]
        self.assertEqual(
            cache.get("total_input_power"),
            1500,
            "initial aggregate should populate total_input_power",
        )

        # Step 2: AC drops, live host packet shows ac_input=0 and dc_input=0.
        # On a standalone PPS the cached total must be re-aggregated to 0
        # instead of holding the stale 1500 forever (which is what bug #48
        # produced before this fix).
        kv_ac_dropped = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 95,
            },
            "ac_data_input_hm": {"ac_input_power": 0},
            "dc_data_input_hm": {"dc_input_power": 0},
        }
        b.publish_state("DEV1", kv_ac_dropped)
        cache = b._state_cache["DEV1"]
        self.assertEqual(cache.get("ac_input_power"), 0)
        self.assertEqual(cache.get("dc_input_power"), 0)
        self.assertEqual(
            cache.get("total_input_power"),
            0,
            "standalone PPS total_input_power must refresh to 0 "
            "when ac_input_power and dc_input_power both drop to 0",
        )

    def test_standalone_pps_total_output_power_refreshes(self):
        """Same pattern for the output side: stale cached total_output_power
        must follow live ac_output_power + dc_output_power on standalone PPS."""
        b = make_bridge()

        kv_initial = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 95,
            },
            "ac_data_output_hm": {"ac_output_power": 800},
            "dc_data_output_hm": {"dc_output_power": 200},
        }
        b.publish_state("DEV1", kv_initial)
        cache = b._state_cache["DEV1"]
        self.assertEqual(
            cache.get("total_output_power"),
            1000,
            "initial aggregate should populate total_output_power",
        )

        kv_load_dropped = {
            "host_packet_data_jdb": {
                "host_packet_voltage": 53.1,
                "host_packet_electric_percentage": 95,
            },
            "ac_data_output_hm": {"ac_output_power": 0},
            "dc_data_output_hm": {"dc_output_power": 0},
        }
        b.publish_state("DEV1", kv_load_dropped)
        cache = b._state_cache["DEV1"]
        self.assertEqual(cache.get("ac_output_power"), 0)
        self.assertEqual(cache.get("dc_output_power"), 0)
        self.assertEqual(
            cache.get("total_output_power"),
            0,
            "standalone PPS total_output_power must refresh to 0 "
            "when ac_output_power and dc_output_power both drop to 0",
        )

    def test_total_power_not_clobbered_with_packs(self):
        """Devices with at least one occupied expansion pack must preserve
        the original 'fill once, don't clobber' behavior: a cached
        total_input_power must NOT be re-aggregated from components on
        subsequent packets, since the canonical top-level total reading is
        authoritative for those models."""
        b = make_bridge()

        connected_pack = {
            "charging_pack_status": 3,  # balanced charging
            "charging_pack_battery": 80,
            "charging_pack_voltage": 53.0,
            "charging_pack_current": 1.2,
            "charging_pack_temp": 28,
        }

        # First packet: top-level total_input_power is the canonical reading
        # for this model. Pack data marks it as non-standalone (one occupied
        # pack is enough to disable the standalone re-aggregation path).
        kv_with_total = {
            "battery_percentage": 85,
            "total_input_power": 1500,
            "ac_data_input_hm": {"ac_input_power": 1200},
            "dc_data_input_hm": {"dc_input_power": 300},
            "charging_pack_data_jdb": [
                connected_pack,
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
            ],
        }
        b.publish_state("DEV1", kv_with_total)
        cache = b._state_cache["DEV1"]
        self.assertEqual(
            cache.get("total_input_power"),
            1500,
            "initial top-level total_input_power should populate cache",
        )

        # Second packet: same pack still occupied, ac/dc drop to 0 but the
        # top-level total isn't re-emitted in this packet. The cached value
        # must NOT be re-aggregated from components (would clobber 1500 with 0).
        kv_packet_two = {
            "battery_percentage": 85,
            "ac_data_input_hm": {"ac_input_power": 0},
            "dc_data_input_hm": {"dc_input_power": 0},
            "charging_pack_data_jdb": [
                connected_pack,
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
                {"charging_pack_status": 4},
            ],
        }
        b.publish_state("DEV1", kv_packet_two)
        cache = b._state_cache["DEV1"]
        self.assertEqual(
            cache.get("total_input_power"),
            1500,
            "cached total_input_power must not be re-aggregated "
            "on devices with occupied expansion packs",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
