#!/usr/bin/env python3
"""Explicit-run, read-only TCP validation for E3800 and E3800LFP devices.

This file remains excluded from ordinary pytest collection by
``tests/hardware/conftest.py``. Run it directly with a local config file.
"""

from __future__ import annotations

import logging
import socket
import struct
import sys
import time
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import local_transport
from constants import SENSOR_FIELDS
from local_transport import LocalTransport
from tests.hardware.config_loader import load_hardware_devices

ALLOWED_MODELS = {"E3800", "E3800LFP"}
ALLOWED_COMMANDS = {0x7032, 0x7034, 0x0011}
CONTROL_COMMAND = 0x0013
READ_WINDOW_SECONDS = 45.0
SOCKET_TIMEOUT_SECONDS = 5.0
RETRY_DELAY_SECONDS = 1.0


class ReadOnlyViolation(RuntimeError):
    """Raised before an outbound packet outside the read-only allowlist is sent."""


def _unstuff_packet(packet: bytes) -> bytes:
    unstuffed = bytearray(packet[:2])
    index = 2
    while index < len(packet):
        if index < len(packet) - 1 and packet[index : index + 2] == b"\xaa\x55":
            unstuffed.append(0xAA)
            index += 2
        else:
            unstuffed.append(packet[index])
            index += 1
    return bytes(unstuffed)


def _command_id(packet: bytes) -> int:
    unstuffed = _unstuff_packet(packet)
    if len(unstuffed) < 9 or unstuffed[:2] != b"\xaa\xaa":
        raise ReadOnlyViolation("refusing malformed outbound local protocol packet")
    declared_length = struct.unpack(">H", unstuffed[2:4])[0]
    if len(unstuffed) < declared_length + 4:
        raise ReadOnlyViolation("refusing truncated outbound local protocol packet")
    return struct.unpack(">H", unstuffed[7:9])[0]


class _GuardedSocket:
    def __init__(self, raw_socket, guard: "OutboundCommandGuard"):
        self._raw_socket = raw_socket
        self._guard = guard
        self._requested_timeout = None

    def _prepare_io(self) -> None:
        remaining = self._guard.remaining()
        if remaining <= 0:
            raise socket.timeout("read-only validation window expired")
        timeout = remaining
        if self._requested_timeout is not None:
            timeout = min(timeout, self._requested_timeout)
        self._raw_socket.settimeout(max(timeout, 0.001))

    def settimeout(self, timeout) -> None:
        self._requested_timeout = timeout
        self._prepare_io()

    def gettimeout(self):
        return self._requested_timeout

    def connect(self, address) -> None:
        self._prepare_io()
        self._raw_socket.connect(address)

    def recv(self, size: int) -> bytes:
        self._prepare_io()
        return self._raw_socket.recv(size)

    def sendall(self, packet: bytes) -> None:
        command = self._guard.record(packet)
        self._prepare_io()
        if command not in ALLOWED_COMMANDS:
            raise ReadOnlyViolation(f"refusing outbound command 0x{command:04X}")
        self._raw_socket.sendall(packet)

    def close(self) -> None:
        self._raw_socket.close()

    def __getattr__(self, name):
        return getattr(self._raw_socket, name)


class OutboundCommandGuard:
    """Record command IDs and prevent any non-handshake/non-read packet."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.command_ids: list[int] = []
        self.violations: list[int] = []
        self._socket_factory = socket.socket

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def record(self, packet: bytes) -> int:
        command = _command_id(packet)
        self.command_ids.append(command)
        if command not in ALLOWED_COMMANDS:
            self.violations.append(command)
        return command

    def socket_factory(self, *args, **kwargs):
        return _GuardedSocket(self._socket_factory(*args, **kwargs), self)

    def assert_read_only(self) -> None:
        if CONTROL_COMMAND in self.violations:
            raise AssertionError("control command 0x0013 was attempted")
        if self.violations:
            commands = ", ".join(f"0x{command:04X}" for command in self.violations)
            raise AssertionError(f"non-read-only outbound command(s) attempted: {commands}")


def _extract_sensor(status: dict, sensor_name: str):
    for path in SENSOR_FIELDS.get(sensor_name, []):
        value = status
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value is not None:
            return value
    return None


def _required_readings(status: dict) -> tuple[object, object, object]:
    return (
        _extract_sensor(status, "battery_percent"),
        _extract_sensor(status, "voltage"),
        _extract_sensor(status, "temperature"),
    )


def _readings_are_complete(readings: tuple[object, object, object]) -> bool:
    battery, voltage, temperature = readings
    return battery is not None and voltage is not None and voltage != 0 and temperature is not None


def validate_device(device: dict) -> bool:
    model = device.get("model")
    if model not in ALLOWED_MODELS:
        raise ValueError("read-only harness requires model E3800LFP or E3800")

    deadline = time.monotonic() + READ_WINDOW_SECONDS
    guard = OutboundCommandGuard(deadline)
    transport = LocalTransport(
        device["lan_ip"],
        device["auth_key"],
        timeout=SOCKET_TIMEOUT_SECONDS,
        multi_packet_timeout=SOCKET_TIMEOUT_SECONDS,
    )
    readings = (None, None, None)
    attempt = 0

    try:
        with patch.object(local_transport.socket, "socket", guard.socket_factory):
            while guard.remaining() > 0:
                attempt += 1
                transport.disconnect()
                if transport.connect():
                    status = transport.read_status()
                    readings = _required_readings(status)
                    if _readings_are_complete(readings):
                        break
                transport.disconnect()
                remaining = guard.remaining()
                if remaining > 0:
                    time.sleep(min(RETRY_DELAY_SECONDS, remaining))
    finally:
        transport.disconnect()

    guard.assert_read_only()
    commands = ", ".join(f"0x{command:04X}" for command in guard.command_ids)
    print(f"{device['device_key']} ({model}): outbound commands [{commands}]")
    battery, voltage, temperature = readings
    if not _readings_are_complete(readings):
        print(
            f"{device['device_key']} ({model}): incomplete readings after {attempt} attempt(s): "
            f"battery={battery!r}, voltage={voltage!r}, temperature={temperature!r}"
        )
        return False

    print(
        f"{device['device_key']} ({model}): battery={battery!r}, "
        f"voltage={voltage!r}, temperature={temperature!r}"
    )
    return True


def main() -> int:
    devices = load_hardware_devices()
    e3800_devices = [device for device in devices if device.get("model") in ALLOWED_MODELS]
    if not e3800_devices:
        print("No configured device has model E3800LFP or E3800", file=sys.stderr)
        return 1

    all_passed = True
    for device in e3800_devices:
        try:
            if not validate_device(device):
                all_passed = False
        except Exception as exc:
            print(f"{device['device_key']} ({device['model']}): FAIL: {exc}", file=sys.stderr)
            all_passed = False
    return 0 if all_passed else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
