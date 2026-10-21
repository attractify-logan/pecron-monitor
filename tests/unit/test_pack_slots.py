"""Expansion-pack slot decoding and deferred HA discovery (v0.8.4).

The Pecron cloud TSL for E1500LFP and E3800LFP defines the
charging_pack_data_jdb element struct as:

    1 charging_pack_num, 2 charging_pack_status, 3 charging_pack_voltage,
    4 charging_pack_current, 5 charging_pack_temp, 6 charging_pack_battery

The LAN decoder had 2 and 6 swapped. Paired readings from an E1500LFP slot 0
with no pack attached, taken seconds apart on 2026-10-21:

    cloud MQTT: battery "1", status "4" (No Connection)
    local TCP : field 2 = 4, field 6 = 1
"""

import json
import logging
from unittest.mock import MagicMock

from ha_bridge import HomeAssistantBridge
from monitor import PecronMonitor
from protocol import _fields_to_kv

PACK_FIELDS = ("battery", "voltage", "current", "temp", "status")


def _local_e1500_pack_fields():
    """E1500LFP charging_pack_data_jdb as the LAN sends it: four slots, none attached."""
    fields = [(36, "STRUCT", 4)]
    slot0 = [(1, "NUM", 0), (2, "NUM", 4), (3, "NUM", 53.18299865722656)]
    slot0 += [(4, "NUM", 0.72000002861023), (5, "NUM", 30), (6, "NUM", 1)]
    fields += [(1, "STRUCT", 6)] + slot0
    for slot in range(2, 5):
        empty = [(1, "NUM", 0), (2, "NUM", 4), (3, "NUM", 0)]
        empty += [(4, "NUM", 0), (5, "NUM", 0), (6, "NUM", 0)]
        fields += [(slot, "STRUCT", 6)] + empty
    return fields


def test_local_pack_struct_matches_cloud_tsl_ids():
    packs = _fields_to_kv(_local_e1500_pack_fields())["charging_pack_data_jdb"]

    assert packs[0]["charging_pack_status"] == 4
    assert packs[0]["charging_pack_battery"] == 1
    assert [p["charging_pack_status"] for p in packs] == [4, 4, 4, 4]
    assert [p["charging_pack_battery"] for p in packs] == [1, 0, 0, 0]


def _monitor():
    m = PecronMonitor.__new__(PecronMonitor)
    m.config = {"alerts": {}, "rules": []}
    m.devices = [{"device_key": "DK", "device_name": "F3000LFP"}]
    m.latest_data = {}
    m.data_sources = {}
    m._last_logged_values = {}
    m.last_alert = {}
    m.last_rule_action = {}
    m.rules = []
    m.ha_bridge = MagicMock()
    return m


def test_process_data_leaves_attached_pack_fields_alone():
    """A real pack that is attached but not charging (status 0) at 80% must
    keep status 0 and battery 80. The old EP3000 swap heuristic, a workaround
    for the LAN decoder bug, rewrote this to status 80 / battery 0."""
    kv = {
        "host_packet_data_jdb": {"host_packet_voltage": 53.2, "host_packet_temp": 25},
        "charging_pack_data_jdb": [
            {
                "charging_pack_status": 0,
                "charging_pack_battery": 80,
                "charging_pack_voltage": 53.2,
            }
        ],
    }

    _monitor()._process_data("DK", kv, source="LOCAL TCP")

    assert kv["charging_pack_data_jdb"][0]["charging_pack_status"] == 0
    assert kv["charging_pack_data_jdb"][0]["charging_pack_battery"] == 80


# -------------------- HA discovery --------------------


def _bridge():
    device = {"device_key": "DK", "device_name": "E1500LFP", "controls": {}}
    b = HomeAssistantBridge(
        {"discovery_prefix": "homeassistant", "clear_discovery_on_startup": False}, [device]
    )
    b.client = MagicMock()
    b._connected = True
    return b


def _publishes(bridge):
    """topic -> list of payloads, in publish order."""
    out = {}
    for call in bridge.client.publish.call_args_list:
        out.setdefault(call.args[0], []).append(call.args[1])
    return out


def _pack_topic(slot, field):
    return f"homeassistant/sensor/pecron_DK/pack_{slot}_{field}/config"


def _pack(status, battery=0):
    return {
        "charging_pack_status": status,
        "charging_pack_battery": battery,
        "charging_pack_voltage": 53.1,
        "charging_pack_current": 0.4,
        "charging_pack_temp": 28,
    }


def test_startup_discovery_clears_pack_configs_instead_of_publishing_them():
    """Installs upgraded from <=0.8.3 hold 20 retained pack configs per PPS.
    Startup discovery must blank every one so HA drops the ghost entities."""
    b = _bridge()

    b._publish_discovery()

    pubs = _publishes(b)
    for slot in range(4):
        for field in PACK_FIELDS:
            assert pubs[_pack_topic(slot, field)] == [""], (slot, field)


def test_pack_discovery_waits_for_a_connected_slot():
    b = _bridge()
    b._publish_discovery()
    b.client.reset_mock()

    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(4)] * 4})
    assert not [t for t in _publishes(b) if t.endswith("/config")]

    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(4), _pack(1, 76), _pack(4), _pack(4)]})
    configs = {t: p for t, p in _publishes(b).items() if t.endswith("/config")}
    assert set(configs) == {_pack_topic(1, f) for f in PACK_FIELDS}
    payload = json.loads(configs[_pack_topic(1, "battery")][-1])
    assert payload["unique_id"] == "pecron_DK_pack_1_battery"
    assert payload["value_template"] == "{{ value_json.pack_1_battery }}"
    assert b._state_cache["DK"]["pack_1_battery"] == 76
    assert b._state_cache["DK"]["pack_1_status"] == "Cascade Charging"

    b.client.reset_mock()
    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(4), _pack(1, 77), _pack(4), _pack(4)]})
    assert not [t for t in _publishes(b) if t.endswith("/config")]


def test_reconnect_keeps_observed_packs_and_ports_registered():
    """A broker reconnect reruns _publish_discovery. Slots and ports already
    seen must be republished, not blanked by the stale-entity sweep."""
    b = _bridge()
    b._publish_discovery()
    b.publish_state(
        "DK",
        {
            "charging_pack_data_jdb": [_pack(3, 90), _pack(4), _pack(4), _pack(4)],
            "dc_data_input_hm": {"gx16mf2_input_voltage": 44.0},
        },
    )
    b.client.reset_mock()

    b._publish_discovery()

    pubs = _publishes(b)
    for field in PACK_FIELDS:
        assert pubs[_pack_topic(0, field)][-1] != "", field
        assert pubs[_pack_topic(1, field)] == [""], field
    gx = "homeassistant/sensor/pecron_DK/gx16mf2_input_voltage/config"
    assert pubs[gx][-1] != ""
    dc = "homeassistant/sensor/pecron_DK/dc5521_input_voltage/config"
    assert pubs[dc] == [""]


def test_pack_discovery_logs_first_observation(caplog):
    b = _bridge()
    b._publish_discovery()
    caplog.set_level(logging.INFO, logger="pecron")
    b.publish_state("DK", {"charging_pack_data_jdb": [_pack(2, 50)]})
    assert any("pack slot 0" in r.message for r in caplog.records)
