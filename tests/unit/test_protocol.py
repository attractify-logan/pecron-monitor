"""
Unit tests for protocol.py — TTLV packet encoding for local TCP/BLE transport.

These are pure-function tests. The packet format is:

    \\xaa\\xaa  [length:2BE]  [crc:1]  [packet_id:2BE]  [cmd:2BE]  [payload...]

Where:
    - length  = len(packet_id + cmd + payload) + 1 (for crc)
    - crc     = sum(packet_id + cmd + payload) & 0xFF
    - payload = varint-encoded data point tags and values

We verify byte-exact output against hand-computed expected values, then add a
minimal decoder to cover round-trip invariants.
"""

import struct
import pytest

from protocol import (
    _encode_varint,
    _build_packet,
    build_ttlv_read,
    build_ttlv_write_bool,
    build_ttlv_write_enum,
)


# ---------------------------------------------------------------------------
# Helper: minimal TTLV packet decoder for round-trip tests
# ---------------------------------------------------------------------------


def decode_packet(packet: bytes) -> dict:
    """Decode a TTLV packet into its components. Raises on malformed input."""
    if len(packet) < 8:
        raise ValueError(f"packet too short: {len(packet)} bytes")
    if packet[:2] != b"\xaa\xaa":
        raise ValueError(f"bad magic: {packet[:2]!r}")

    length = struct.unpack(">H", packet[2:4])[0]
    crc = packet[4]
    inner = packet[5 : 5 + length - 1]  # length includes the crc byte

    if len(inner) != length - 1:
        raise ValueError(f"length mismatch: declared {length - 1}, got {len(inner)}")

    computed_crc = sum(inner) & 0xFF
    if computed_crc != crc:
        raise ValueError(f"bad crc: declared 0x{crc:02x}, computed 0x{computed_crc:02x}")

    packet_id, cmd = struct.unpack(">HH", inner[:4])
    payload = inner[4:]
    return {
        "length": length,
        "crc": crc,
        "packet_id": packet_id,
        "cmd": cmd,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# _encode_varint — big-endian byte-packed integer
# ---------------------------------------------------------------------------


class TestEncodeVarint:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80"),
            (255, b"\xff"),
            (256, b"\x01\x00"),
            (0xABCD, b"\xab\xcd"),
            (0xFF00FF, b"\xff\x00\xff"),
        ],
    )
    def test_encoding(self, value, expected):
        assert _encode_varint(value) == expected

    def test_zero_is_single_byte(self):
        assert _encode_varint(0) == b"\x00"
        assert len(_encode_varint(0)) == 1

    def test_monotonic_length(self):
        """Larger values should never encode shorter than smaller values."""
        lens = [len(_encode_varint(2**i)) for i in range(0, 33, 4)]
        assert lens == sorted(lens)


# ---------------------------------------------------------------------------
# _build_packet — frames header/crc around an (id, cmd, payload) triple
# ---------------------------------------------------------------------------


class TestBuildPacket:
    def test_minimal_packet_structure(self):
        # packet_id=1, cmd=0x0011, no payload
        #   inner = 00 01 00 11 (4 bytes)
        #   crc   = (0+1+0+0x11) & 0xff = 0x12
        #   length = 4 + 1 = 5
        pkt = _build_packet(packet_id=1, cmd=0x0011)
        expected = b"\xaa\xaa\x00\x05\x12\x00\x01\x00\x11"
        assert pkt == expected

    def test_with_payload(self):
        # packet_id=2, cmd=0x0013, payload=b"\xc8\x01"
        #   inner = 00 02 00 13 c8 01
        #   crc   = (0+2+0+0x13+0xc8+1) & 0xff = 0xde
        #   length = 6 + 1 = 7
        pkt = _build_packet(packet_id=2, cmd=0x0013, payload=b"\xc8\x01")
        expected = b"\xaa\xaa\x00\x07\xde\x00\x02\x00\x13\xc8\x01"
        assert pkt == expected

    def test_packet_starts_with_magic(self):
        pkt = _build_packet(1, 0x0011)
        assert pkt[:2] == b"\xaa\xaa"

    def test_length_field_matches_inner(self):
        pkt = _build_packet(1, 0x0013, payload=b"\xde\xad\xbe\xef")
        declared_length = struct.unpack(">H", pkt[2:4])[0]
        # length = len(inner) + 1 (crc). inner = 2+2+4 = 8, length = 9
        assert declared_length == 9
        # Actual bytes after the length+crc header should be declared_length - 1
        assert len(pkt[5:]) == declared_length - 1

    def test_crc_matches(self):
        pkt = _build_packet(1, 0x0013, payload=b"\x10\x20\x30")
        decoded = decode_packet(pkt)
        # Just verifying decode_packet's crc check doesn't raise
        assert decoded["packet_id"] == 1
        assert decoded["cmd"] == 0x0013
        assert decoded["payload"] == b"\x10\x20\x30"

    def test_empty_payload_is_default(self):
        pkt1 = _build_packet(1, 0x0011)
        pkt2 = _build_packet(1, 0x0011, payload=b"")
        assert pkt1 == pkt2


# ---------------------------------------------------------------------------
# build_ttlv_read — cmd=0x0011 status request
# ---------------------------------------------------------------------------


class TestBuildTtlvRead:
    def test_default_packet_id(self):
        pkt = build_ttlv_read()
        decoded = decode_packet(pkt)
        assert decoded["packet_id"] == 1
        assert decoded["cmd"] == 0x0011
        assert decoded["payload"] == b""

    def test_custom_packet_id(self):
        pkt = build_ttlv_read(packet_id=42)
        decoded = decode_packet(pkt)
        assert decoded["packet_id"] == 42
        assert decoded["cmd"] == 0x0011

    def test_byte_exact(self):
        # Hand-computed: same as TestBuildPacket.test_minimal_packet_structure
        assert build_ttlv_read(1) == b"\xaa\xaa\x00\x05\x12\x00\x01\x00\x11"

    def test_starts_with_magic(self):
        assert build_ttlv_read()[:2] == b"\xaa\xaa"


# ---------------------------------------------------------------------------
# build_ttlv_write_bool — cmd=0x0013 bool write
# ---------------------------------------------------------------------------


class TestBuildTtlvWriteBool:
    def test_ac_switch_on(self):
        # ac_switch_hm has data_point_id=40 (from DEFAULT_CONTROLS)
        # tag = (40 << 3) | 1 = 0x141
        # varint(0x141) = b"\x01\x41"
        pkt = build_ttlv_write_bool(packet_id=1, data_point_id=40, value=True)
        decoded = decode_packet(pkt)
        assert decoded["cmd"] == 0x0013
        assert decoded["packet_id"] == 1
        assert decoded["payload"] == b"\x01\x41"

    def test_ac_switch_off(self):
        # tag = (40 << 3) | 0 = 0x140
        pkt = build_ttlv_write_bool(packet_id=1, data_point_id=40, value=False)
        decoded = decode_packet(pkt)
        assert decoded["payload"] == b"\x01\x40"

    def test_dc_switch_on(self):
        # dc_switch_hm has data_point_id=38
        # tag = (38 << 3) | 1 = 0x131
        pkt = build_ttlv_write_bool(packet_id=5, data_point_id=38, value=True)
        decoded = decode_packet(pkt)
        assert decoded["packet_id"] == 5
        assert decoded["payload"] == b"\x01\x31"

    def test_ups_off(self):
        # ups_status_hm has data_point_id=27
        # tag = (27 << 3) | 0 = 0xd8
        pkt = build_ttlv_write_bool(packet_id=1, data_point_id=27, value=False)
        decoded = decode_packet(pkt)
        assert decoded["payload"] == b"\xd8"

    def test_truthy_bit_encoding(self):
        # Low bit of tag encodes the bool value
        on = build_ttlv_write_bool(1, 40, True)
        off = build_ttlv_write_bool(1, 40, False)
        # The last byte of payload should differ by 1 (low bit)
        on_decoded = decode_packet(on)
        off_decoded = decode_packet(off)
        assert on_decoded["payload"][-1] - off_decoded["payload"][-1] == 1


# ---------------------------------------------------------------------------
# build_ttlv_write_enum — cmd=0x0013 enum/int write
# ---------------------------------------------------------------------------


class TestBuildTtlvWriteEnum:
    def test_basic(self):
        # data_point_id=45 (screen brightness), value=3 (80%)
        # tag = (45 << 3) | 2 = 0x16a
        # varint(0x16a) = b"\x01\x6a"
        # varint(3) = b"\x03"
        pkt = build_ttlv_write_enum(packet_id=1, data_point_id=45, value=3)
        decoded = decode_packet(pkt)
        assert decoded["cmd"] == 0x0013
        assert decoded["payload"] == b"\x01\x6a\x03"

    def test_zero_value(self):
        # tag = (45 << 3) | 2 = 0x16a; value 0 still encoded as b"\x00"
        pkt = build_ttlv_write_enum(1, 45, 0)
        decoded = decode_packet(pkt)
        assert decoded["payload"] == b"\x01\x6a\x00"

    def test_large_value(self):
        # data_point_id=50, value=0xABCD
        # tag = (50 << 3) | 2 = 0x192
        pkt = build_ttlv_write_enum(1, 50, 0xABCD)
        decoded = decode_packet(pkt)
        assert decoded["payload"] == b"\x01\x92\xab\xcd"

    def test_type_bit_is_2(self):
        # Enum/int encodes type=2 in the low 3 bits of the tag value.
        # Since varints are big-endian, those bits land in the LAST byte of the
        # tag encoding. For dp=40: tag = (40<<3)|2 = 0x142 = varint b"\x01\x42",
        # so the type bits are in payload[1] & 7 == 2.
        pkt = build_ttlv_write_enum(1, 40, 5)
        decoded = decode_packet(pkt)
        # Reconstruct the tag from its varint encoding (2 bytes for dp=40)
        tag_bytes = decoded["payload"][:2]
        tag = int.from_bytes(tag_bytes, "big")
        assert tag & 0x07 == 2
        assert tag >> 3 == 40


# ---------------------------------------------------------------------------
# Round-trip invariants
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("packet_id", [1, 2, 100, 0xFFFE])
    def test_read_roundtrip(self, packet_id):
        pkt = build_ttlv_read(packet_id)
        decoded = decode_packet(pkt)
        assert decoded["packet_id"] == packet_id
        assert decoded["cmd"] == 0x0011

    @pytest.mark.parametrize(
        "packet_id,dp_id,value",
        [
            (1, 27, True),
            (1, 27, False),
            (42, 38, True),
            (100, 40, False),
            (0xABCD, 91, True),
        ],
    )
    def test_write_bool_roundtrip(self, packet_id, dp_id, value):
        pkt = build_ttlv_write_bool(packet_id, dp_id, value)
        decoded = decode_packet(pkt)
        assert decoded["packet_id"] == packet_id
        assert decoded["cmd"] == 0x0013
        # Last byte of payload encodes the bool bit
        assert (decoded["payload"][-1] & 1) == (1 if value else 0)

    @pytest.mark.parametrize(
        "packet_id,dp_id,value",
        [
            (1, 45, 0),
            (1, 45, 3),
            (1, 50, 100),
            (42, 91, 0xABCD),
        ],
    )
    def test_write_enum_roundtrip(self, packet_id, dp_id, value):
        pkt = build_ttlv_write_enum(packet_id, dp_id, value)
        decoded = decode_packet(pkt)
        assert decoded["packet_id"] == packet_id
        assert decoded["cmd"] == 0x0013

    def test_all_packets_start_with_magic(self):
        assert build_ttlv_read()[:2] == b"\xaa\xaa"
        assert build_ttlv_write_bool(1, 40, True)[:2] == b"\xaa\xaa"
        assert build_ttlv_write_enum(1, 45, 3)[:2] == b"\xaa\xaa"


# ---------------------------------------------------------------------------
# Golden fixtures — swap in real device captures here
# ---------------------------------------------------------------------------


class TestGoldenFixtures:
    """TODO: Replace these synthetic expected-bytes with real captures sniffed
    from a live device. To capture:
        1. Run `python3 pecron_monitor.py --status` against a real device
        2. tcpdump or wireshark port 6607 (TCP) or the BLE characteristic
        3. Paste the raw hex into tests/fixtures/ttlv_packets/<name>.hex
        4. Load it here and compare against build_ttlv_read() / write_bool() / write_enum()
    """

    def test_status_request_matches_golden(self, ttlv_packets_dir):
        golden = ttlv_packets_dir / "status_request.hex"
        if not golden.exists():
            pytest.skip("No real capture yet — add tests/fixtures/ttlv_packets/status_request.hex")
        expected = bytes.fromhex(golden.read_text().strip())
        # packet_id in the capture may vary; compare the framing structure
        decoded_golden = decode_packet(expected)
        decoded_ours = decode_packet(build_ttlv_read(decoded_golden["packet_id"]))
        assert decoded_ours["cmd"] == decoded_golden["cmd"] == 0x0011
        assert decoded_ours["payload"] == decoded_golden["payload"]
