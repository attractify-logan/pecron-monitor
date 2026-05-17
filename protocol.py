"""
TTLV protocol functions for pecron-monitor.

Provides packet building functions for local TCP/BLE communication with
Pecron devices using the TTLV (Tag-Type-Length-Value) protocol.
"""

import struct


def _encode_varint(val: int) -> bytes:
    if val == 0:
        return b"\x00"
    result = []
    while val > 0:
        result.append(val & 0xFF)
        val >>= 8
    return bytes(reversed(result))


def _build_packet(packet_id: int, cmd: int, payload: bytes = b"") -> bytes:
    inner = struct.pack(">HH", packet_id, cmd) + payload
    crc = sum(inner) & 0xFF
    length = len(inner) + 1
    return b"\xaa\xaa" + struct.pack(">H", length) + bytes([crc]) + inner


def build_ttlv_read(packet_id: int = 1) -> bytes:
    """cmd=0x0011: request device status."""
    return _build_packet(packet_id, 0x0011)


def build_ttlv_write_bool(packet_id: int, data_point_id: int, value: bool) -> bytes:
    """cmd=0x0013: write a boolean data point."""
    tag = (data_point_id << 3) | (1 if value else 0)
    payload = _encode_varint(tag)
    return _build_packet(packet_id, 0x0013, payload)


def build_ttlv_write_enum(packet_id: int, data_point_id: int, value: int) -> bytes:
    """cmd=0x0013: write an enum/int data point."""
    tag = (data_point_id << 3) | 2  # type 2 = number
    payload = _encode_varint(tag) + _encode_varint(value)
    return _build_packet(packet_id, 0x0013, payload)
