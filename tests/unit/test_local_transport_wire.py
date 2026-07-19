"""Wire-level characterization for the local TCP transport (issue #66)."""

import base64
import hashlib
import socket
import struct
from unittest.mock import Mock, patch

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from local_transport import LocalTransport


FAKE_KEY = bytes(range(16))
FAKE_KEY_B64 = base64.b64encode(FAKE_KEY).decode("ascii")
FIXED_IV = bytes(range(16, 32))
HANDSHAKE_IV = b"0123456789abcdef"


class ScriptedSocket:
    """A deterministic socket whose reads may be shorter than requested."""

    def __init__(self, incoming=b"", timeout=10.0, max_recv_size=None):
        self.incoming = bytearray(incoming)
        self.timeout = timeout
        self.max_recv_size = max_recv_size
        self.sent = []
        self.timeout_history = []
        self.connected_to = None
        self.closed = False
        self.recv_sizes = []

    def recv(self, size):
        self.recv_sizes.append(size)
        if not self.incoming:
            return b""
        count = min(size, len(self.incoming))
        if self.max_recv_size is not None:
            count = min(count, self.max_recv_size)
        result = bytes(self.incoming[:count])
        del self.incoming[:count]
        return result

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeout_history.append(timeout)

    def gettimeout(self):
        return self.timeout

    def connect(self, address):
        self.connected_to = address

    def close(self):
        self.closed = True


def _stuff(raw):
    stuffed = bytearray(raw[:2])
    index = 2
    while index < len(raw):
        stuffed.append(raw[index])
        if index < len(raw) - 1 and raw[index] == 0xAA and raw[index + 1] in (0x55, 0xAA):
            stuffed.append(0x55)
        index += 1
    return bytes(stuffed)


def _packet(command, payload=b"", packet_id=1):
    inner = struct.pack(">HH", packet_id, command) + payload
    raw = b"\xaa\xaa" + struct.pack(">H", len(inner) + 1) + bytes([sum(inner) & 0xFF]) + inner
    return _stuff(raw)


def _bytes_field(field_id, value):
    return struct.pack(">HH", (field_id << 3) | 3, len(value)) + value


def _number_field(field_id, value):
    return struct.pack(">HBB", (field_id << 3) | 2, 0, value)


def _encrypted_payload(plaintext, key=FAKE_KEY, iv=FIXED_IV):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, 16))


def _connected_transport(sock=None, multi_packet_timeout=3.0):
    transport = LocalTransport(
        "192.0.2.10",
        FAKE_KEY_B64,
        multi_packet_timeout=multi_packet_timeout,
    )
    transport._sock = sock or ScriptedSocket()
    transport._iv = FIXED_IV
    transport._encrypted = True
    transport._connected = True
    return transport


def test_receive_packet_discards_garbage_and_reassembles_fragmented_wire_packet():
    expected = bytes.fromhex("aaaa00081c00020014010203")
    sock = ScriptedSocket(b"\x00\x7f\xaa\x00garbage" + expected, max_recv_size=2)
    transport = _connected_transport(sock)

    received = transport._recv_packet()

    assert received == expected
    assert sock.incoming == b""
    assert any(size > 2 for size in sock.recv_sizes)


def test_fixed_key_and_iv_aes_cbc_vector_round_trips():
    transport = _connected_transport()
    plaintext = b"Pecron wire test"

    ciphertext = transport._encrypt(plaintext)

    assert ciphertext == bytes.fromhex(
        "5dc40d713df6868c2528122d143f0623f41b811ad19e78a34fb58a5b460aec2b"
    )
    assert transport._decrypt(ciphertext) == plaintext


def _random_response(command=0x7033, include_random=True):
    payload = _bytes_field(1, HANDSHAKE_IV) if include_random else b""
    return _packet(command, payload, packet_id=0x1001)


def _login_response(command=0x7035, result=0):
    return _packet(command, _number_field(3, result), packet_id=0x1002)


def test_handshake_sends_random_then_hashed_login_and_enters_encrypted_state():
    sock = ScriptedSocket(_random_response() + _login_response(), max_recv_size=3)
    transport = LocalTransport("192.0.2.10", FAKE_KEY_B64)

    with patch("local_transport.socket.socket", return_value=sock) as socket_factory:
        assert transport.connect() is True

    socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
    assert sock.connected_to == ("192.0.2.10", 6607)
    assert sock.sent == [
        bytes.fromhex("aaaa0005a300017032"),
        bytes.fromhex(
            "aaaa00492f0002703400130040"
            "3061383061646631346230313765356163306131626266393537626464336234"
            "3736376535373465653761643066303565626661343139643534343534653834"
        ),
    ]
    expected_hash = (
        hashlib.sha256(b"000102030405060708090a0b0c0d0e0f;0123456789abcdef")
        .hexdigest()
        .encode("ascii")
    )
    assert expected_hash in sock.sent[1]
    assert transport._iv == HANDSHAKE_IV
    assert transport._encrypted is True
    assert transport._connected is True
    assert transport.connected is True
    assert transport._has_connected_once is True


@pytest.mark.parametrize(
    ("responses", "sent_count"),
    [
        (_random_response(command=0x7000), 1),
        (_random_response(include_random=False), 1),
        (_random_response() + _login_response(command=0x7000), 2),
        (_random_response() + _login_response(result=1), 2),
    ],
    ids=["wrong-random-command", "missing-random", "wrong-login-command", "login-rejected"],
)
def test_handshake_failures_disconnect_and_clear_encryption_state(responses, sent_count):
    sock = ScriptedSocket(responses, max_recv_size=4)
    transport = LocalTransport("192.0.2.10", FAKE_KEY_B64)

    with patch("local_transport.socket.socket", return_value=sock):
        assert transport.connect() is False

    assert len(sock.sent) == sent_count
    assert sock.sent[0] == bytes.fromhex("aaaa0005a300017032")
    if sent_count == 2:
        assert sock.sent[1][7:9] == b"\x70\x34"
    assert transport.connected is False
    assert transport._connected is False
    assert transport._encrypted is False
    assert transport._iv is None
    assert transport._sock is None
    assert sock.closed is True


def test_e3800_five_second_timeout_collects_and_merges_encrypted_status_packets():
    sock = ScriptedSocket(timeout=10.0)
    transport = _connected_transport(sock, multi_packet_timeout=5.0)
    transport._first_read_done = True
    ack = _packet(0x0012, packet_id=0x2001)
    battery = _packet(0x0014, _encrypted_payload(bytes.fromhex("000a004b")), 0x2002)
    dc_enabled = _packet(0x0014, _encrypted_payload(bytes.fromhex("0131")), 0x2003)
    transport._recv_packet = Mock(side_effect=[ack, battery, dc_enabled, socket.timeout()])

    status = transport.read_status()

    assert transport.multi_packet_timeout == 5.0
    assert status == {"battery_percentage": 75, "dc_switch_hm": True}
    assert sock.sent == [bytes.fromhex("aaaa00051200010011")]
    assert sock.timeout_history == [5.0, 10.0]
    assert transport._recv_packet.call_count == 4


def test_empty_first_read_retries_once_and_returns_second_read_fields():
    sock = ScriptedSocket(timeout=10.0)
    transport = _connected_transport(sock, multi_packet_timeout=5.0)
    transport._first_read_done = True
    ack = _packet(0x0012, packet_id=0x2101)
    battery = _packet(0x0014, _encrypted_payload(bytes.fromhex("000a0050")), 0x2102)
    transport._recv_packet = Mock(
        side_effect=[ack, socket.timeout(), ack, battery, socket.timeout()]
    )

    with patch("local_transport.time.sleep") as sleep:
        status = transport.read_status()

    assert status == {"battery_percentage": 80}
    assert sock.sent == [
        bytes.fromhex("aaaa00051200010011"),
        bytes.fromhex("aaaa00051300020011"),
    ]
    assert sock.timeout_history == [5.0, 10.0, 5.0, 10.0]
    sleep.assert_called_once_with(1.0)
    assert transport._retried_read is True


@pytest.mark.parametrize(
    ("control_type", "data_point_id", "value", "expected_plaintext"),
    [
        ("BOOL", 38, True, bytes.fromhex("0131")),
        ("BOOL", 38, False, bytes.fromhex("0130")),
        ("ENUM", 50, 2, bytes.fromhex("019202")),
    ],
)
def test_control_plaintext_and_write_frame_are_exact(
    control_type, data_point_id, value, expected_plaintext
):
    sock = ScriptedSocket()
    transport = _connected_transport(sock)
    transport._encrypt = Mock(return_value=bytes.fromhex("deadbeef"))
    transport._recv_packet = Mock(return_value=_packet(0x0014, packet_id=0x3001))

    assert transport._send_control_packet(data_point_id, value, control_type) is True

    transport._encrypt.assert_called_once_with(expected_plaintext)
    assert sock.sent == [bytes.fromhex("aaaa00094c00010013deadbeef")]
    assert sock.sent[0][7:9] == b"\x00\x13"
    assert transport._connected is True
