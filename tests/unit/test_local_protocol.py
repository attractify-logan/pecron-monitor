"""Characterization tests for the local TTLV codec and TSL translation."""

import pytest

from protocol import (
    _build_packet as _build_unstuffed_packet,
    _fields_to_kv,
    _ttlv_build_bytes_field,
    _ttlv_build_packet,
    _ttlv_byte_stuff,
    _ttlv_byte_unstuff,
    _ttlv_parse_fields,
    _ttlv_parse_packet,
)


def test_byte_stuffing_and_unstuffing_preserve_aa_aa_aa55_payload():
    raw = b"\xaa\xaa\xaa\xaa\xaa\x55"
    stuffed = b"\xaa\xaa\xaa\x55\xaa\x55\xaa\x55\x55"

    assert _ttlv_byte_stuff(raw) == stuffed
    assert _ttlv_byte_unstuff(stuffed) == raw


def test_local_status_packet_has_exact_wire_bytes():
    assert _ttlv_build_packet(cmd=0x0011, packet_id=1) == (b"\xaa\xaa\x00\x05\x12\x00\x01\x00\x11")


def test_bytes_field_has_exact_tag_length_and_binary_data():
    assert _ttlv_build_bytes_field(0x123, b"\x00\xaa\xff") == (b"\x09\x1b\x00\x03\x00\xaa\xff")


def test_stuffed_packet_parses_back_to_original_fields():
    payload = b"\xaa\xaa\xaa\x55"
    packet = _ttlv_build_packet(cmd=0x0013, payload=payload, packet_id=0x1234)

    assert packet == (b"\xaa\xaa\x00\x09\xac\x12\x34\x00\x13\xaa\x55\xaa\x55\xaa\x55\x55")
    assert _ttlv_parse_packet(packet) == {
        "cmd": 0x0013,
        "packet_id": 0x1234,
        "payload": payload,
    }


def test_protocol_packet_builder_remains_unstuffed_in_contrast_to_local_codec():
    payload = b"\xaa\xaa\xaa\x55"

    local_packet = _ttlv_build_packet(0x0013, payload, packet_id=0x1234)
    protocol_packet = _build_unstuffed_packet(0x1234, 0x0013, payload)

    assert protocol_packet == (b"\xaa\xaa\x00\x09\xac\x12\x34\x00\x13\xaa\xaa\xaa\x55")
    assert local_packet != protocol_packet
    assert _ttlv_byte_unstuff(local_packet) == protocol_packet


def test_field_parser_decodes_bool_number_bytes_and_struct_values():
    payload = (
        b"\x00\x08"  # id=1, BOOL false
        b"\x00\x11"  # id=2, BOOL true
        b"\x00\x1a\x11\x30\x39"  # id=3, NUM 123.45
        b"\x00\x22\x81\x01\x02"  # id=4, NUM -258
        b"\x00\x2b\x00\x02\x00\xff"  # id=5, BYTES (type 3)
        b"\x00\x35\x00\x02\xc3\xa9"  # id=6, BYTES (type 5)
        b"\x00\x3c\x00\x02"  # id=7, STRUCT with two children
    )

    assert _ttlv_parse_fields(payload) == [
        (1, "BOOL", False),
        (2, "BOOL", True),
        (3, "NUM", 123.45),
        (4, "NUM", -258),
        (5, "BYTES", b"\x00\xff"),
        (6, "BYTES", b"\xc3\xa9"),
        (7, "STRUCT", 2),
    ]


@pytest.mark.parametrize(
    "payload, expected",
    [
        (b"\x00", []),  # incomplete tag
        (b"\x00\x0a", []),  # number missing metadata
        (b"\x00\x0a\x01\xff", []),  # two-byte number with one byte present
        (b"\x00\x0b\x00", []),  # bytes field missing length byte
        (b"\x00\x0b\x00\x04\xde\xad", [(1, "BYTES", b"\xde\xad")]),
        (b"\x00\x0c\x00", []),  # struct missing count byte
    ],
)
def test_field_parser_truncation_behavior(payload, expected):
    assert _ttlv_parse_fields(payload) == expected


def test_tsl_translation_uses_dynamic_control_ids_and_overrides_static_ids():
    fields = [
        (999, "NUM", 7),
        (1, "NUM", 25),
        (998, "BOOL", True),
    ]
    controls = {
        "custom_limit": {"id": 999, "type": "NUM"},
        "renamed_battery": {"id": 1, "type": "NUM"},
        "invalid_control": {"id": "998", "type": "BOOL"},
    }

    assert _fields_to_kv(fields, controls) == {
        "custom_limit": 7,
        "renamed_battery": 25,
    }


def test_tsl_translation_rebuilds_nested_host_packet_data():
    fields = [
        (35, "STRUCT", 6),
        (1, "NUM", 81),
        (2, "NUM", 51.2),
        (3, "NUM", -4.5),
        (4, "NUM", 23),
        (5, "NUM", 3),
        (99, "BOOL", True),
    ]

    assert _fields_to_kv(fields) == {
        "host_packet_data_jdb": {
            "host_packet_electric_percentage": 81,
            "host_packet_voltage": 51.2,
            "host_packet_current": -4.5,
            "host_packet_temp": 23,
            "host_packet_status": 3,
            "field_99": True,
        }
    }


def test_tsl_translation_collects_nested_charging_pack_structs():
    fields = [
        (36, "STRUCT", 2),
        (1, "STRUCT", 3),
        (1, "NUM", 1),
        (2, "NUM", 76),
        (3, "NUM", 48.6),
        (2, "STRUCT", 3),
        (1, "NUM", 2),
        (2, "NUM", 64),
        (99, "BYTES", b"\x01\x02"),
    ]

    assert _fields_to_kv(fields) == {
        "charging_pack_data_jdb": [
            {
                "charging_pack_num": 1,
                "charging_pack_battery": 76,
                "charging_pack_voltage": 48.6,
            },
            {
                "charging_pack_num": 2,
                "charging_pack_battery": 64,
                "field_99": b"\x01\x02",
            },
        ]
    }


def test_tsl_translation_skips_unknown_top_level_ids():
    assert _fields_to_kv([(4095, "NUM", 12), (1, "NUM", 88)]) == {"battery_percentage": 88}


def test_tsl_translation_decodes_utf8_and_hex_encodes_binary_bytes():
    fields = [
        (52, "BYTES", "用户手册".encode("utf-8")),
        (100, "BYTES", b"\xff\x00\xfe"),
    ]

    assert _fields_to_kv(fields) == {
        "device_manual": "用户手册",
        "high_frequency_reporting": "ff00fe",
    }
