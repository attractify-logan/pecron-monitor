"""Characterization tests for the gatttool-based BLE transport."""

import base64
import struct
from unittest.mock import MagicMock, call

import pytest

import ble_transport as transport_module
from ble_transport import BLETransport
from protocol import _ttlv_build_bytes_field, _ttlv_build_packet


AUTH_KEY_B64 = base64.b64encode(b"0123456789abcdef").decode("ascii")
DEVICE_ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeTimeout(Exception):
    """pexpect-compatible timeout raised by deterministic fakes."""


class FakeEOF(Exception):
    """pexpect-compatible EOF raised by deterministic fakes."""


class FakePexpectModule:
    TIMEOUT = FakeTimeout
    EOF = FakeEOF

    def __init__(self, child=None):
        self.child = child
        self.spawn_calls = []

    def spawn(self, command, **kwargs):
        self.spawn_calls.append((command, kwargs))
        if self.child is None:
            raise AssertionError("unexpected gatttool spawn")
        return self.child


class CollectingGatttool:
    """Minimal pexpect child that emits indications then terminates collection."""

    def __init__(self, indications, terminator):
        self.before = ""
        self.after = ""
        self._indications = list(indications)
        self._terminator = terminator
        self.expect_calls = []

    def expect(self, pattern, timeout):
        self.expect_calls.append((pattern, timeout))
        if not self._indications:
            raise self._terminator()
        self.before, self.after = self._indications.pop(0)
        return 0


class ScriptedGatttool:
    """Interactive gatttool fake with scripted expect results."""

    def __init__(self, expect_results):
        self.before = ""
        self.after = ""
        self._expect_results = list(expect_results)
        self.sent = []
        self.expect_calls = []
        self.close_calls = []

    def sendline(self, command):
        self.sent.append(command)

    def expect(self, pattern, timeout):
        self.expect_calls.append((pattern, timeout))
        result = self._expect_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self, force=False):
        self.close_calls.append(force)

    def isalive(self):
        return True


@pytest.fixture(autouse=True)
def hermetic_ble(monkeypatch):
    """Make BLE construction independent of optional pexpect and remove waits."""
    fake_pexpect = FakePexpectModule()
    monkeypatch.setattr(transport_module, "HAS_BLE", True)
    monkeypatch.setattr(transport_module, "pexpect", fake_pexpect, raising=False)
    monkeypatch.setattr(transport_module.time, "sleep", MagicMock())
    return fake_pexpect


def make_transport(controls=None):
    instance = BLETransport(
        auth_key_b64=AUTH_KEY_B64,
        device_address=DEVICE_ADDRESS,
        device_key="TESTDEVICE0001",
        controls=controls,
    )
    instance._iv = b"0123456789abcdef"
    instance._connected = True
    instance._encrypted = True
    return instance


def number_field(field_id, value):
    """Encode a one-byte, unsigned TTLV numeric field."""
    return struct.pack(">H", (field_id << 3) | 2) + b"\x00" + bytes([value])


def bool_field(field_id, value):
    return struct.pack(">H", (field_id << 3) | (1 if value else 0))


class TestIndicationCodec:
    def test_splits_concatenated_indications_into_complete_packets(self):
        instance = make_transport()
        first = _ttlv_build_packet(0x7035, b"first", packet_id=7)
        second = _ttlv_build_packet(0x0014, b"second", packet_id=8)

        parsed = instance._parse_all_packets(b"noise" + first + second + b"trailing")

        assert parsed == [
            {"cmd": 0x7035, "packet_id": 7, "payload": b"first"},
            {"cmd": 0x0014, "packet_id": 8, "payload": b"second"},
        ]

    def test_extracts_iv_from_bytes_field_in_handshake_packet(self):
        instance = make_transport()
        payload = _ttlv_build_bytes_field(1, b"0123456789abcdef")
        packet = _ttlv_build_packet(0x7033, payload, packet_id=12)

        assert instance._extract_iv(packet) == "0123456789abcdef"

    @pytest.mark.parametrize("terminator", [FakeTimeout, FakeEOF])
    def test_collection_keeps_all_indications_when_expect_terminates(self, terminator):
        instance = make_transport()
        child = CollectingGatttool(
            [
                ("gatt prompt\r\n", "value: aa 01 02\r\n"),
                ("notification\r\n", "value: 03 04\r\n"),
            ],
            terminator,
        )
        instance._gt = child

        collected = instance._collect_indications(wait=0, extra_wait=1.25)

        assert collected == b"\xaa\x01\x02\x03\x04"
        assert child.expect_calls == [(r"value:.*", 1.25)] * 3


class TestBleScan:
    def test_filters_prefix_and_deduplicates_addresses(self, monkeypatch):
        stdout = b"\n".join(
            [
                b"AA:AA:AA:AA:AA:01 QUEC_BLE_E1500",
                b"AA:AA:AA:AA:AA:02 unrelated",
                b"AA:AA:AA:AA:AA:01 QUEC_BLE_E1500_DUPLICATE",
                b"malformed",
                b"AA:AA:AA:AA:AA:03 QUEC_BLE_E300",
            ]
        )
        process = MagicMock()
        process.communicate.return_value = (stdout, b"ignored diagnostic")
        popen = MagicMock(return_value=process)
        monkeypatch.setattr(transport_module.subprocess, "Popen", popen)

        found = transport_module.scan_ble_devices(timeout=2.5)

        assert found == [
            ("AA:AA:AA:AA:AA:01", "QUEC_BLE_E1500"),
            ("AA:AA:AA:AA:AA:03", "QUEC_BLE_E300"),
        ]
        popen.assert_called_once_with(
            ["hcitool", "lescan", "--duplicates"],
            stdout=transport_module.subprocess.PIPE,
            stderr=transport_module.subprocess.PIPE,
        )
        process.kill.assert_called_once_with()
        process.communicate.assert_called_once_with()
        transport_module.time.sleep.assert_called_once_with(2.5)


class TestStatusRead:
    def test_empty_first_collection_retries_status_request_once(self):
        instance = make_transport()
        instance._write_and_expect = MagicMock(return_value=True)
        instance._collect_indications = MagicMock(side_effect=[b"", b"captured response"])
        instance._parse_all_packets = MagicMock(
            return_value=[{"cmd": 0x0012, "packet_id": 2, "payload": b"P" * 16}]
        )
        instance._decrypt = MagicMock(return_value=number_field(1, 64))

        status = instance.read_status()

        assert status == {"battery_percentage": 64}
        assert instance._write_and_expect.call_args_list == [
            call("aaaa00051200010011"),
            call("aaaa00051300020011"),
        ]
        assert instance._collect_indications.call_args_list == [
            call(wait=5, extra_wait=5),
            call(wait=6, extra_wait=5),
        ]

    def test_merges_decoded_fields_from_multiple_response_packets(self):
        instance = make_transport()
        instance._write_and_expect = MagicMock(return_value=True)
        first = _ttlv_build_packet(0x0012, b"A" * 16, packet_id=21)
        second = _ttlv_build_packet(0x0014, b"B" * 16, packet_id=22)
        instance._collect_indications = MagicMock(return_value=first + second)
        instance._decrypt = MagicMock(
            side_effect=[number_field(1, 77), bool_field(38, True) + bool_field(40, False)]
        )

        status = instance.read_status()

        assert status == {
            "battery_percentage": 77,
            "dc_switch_hm": True,
            "ac_switch_hm": False,
        }
        assert instance._decrypt.call_args_list == [call(b"A" * 16), call(b"B" * 16)]

    def test_falls_back_to_unencrypted_fields_when_decryption_fails(self):
        instance = make_transport()
        instance._write_and_expect = MagicMock(return_value=True)
        payload = bool_field(40, True) + b"".join(
            bool_field(field_id, False) for field_id in range(200, 207)
        )
        assert len(payload) == 16
        packet = _ttlv_build_packet(0x0014, payload, packet_id=9)
        instance._collect_indications = MagicMock(return_value=packet)
        instance._decrypt = MagicMock(side_effect=ValueError("not encrypted"))

        status = instance.read_status()

        assert status == {"ac_switch_hm": True}
        instance._decrypt.assert_called_once_with(payload)

    def test_unexpected_status_failure_marks_transport_disconnected(self):
        instance = make_transport()
        instance._write_and_expect = MagicMock(return_value=True)
        instance._collect_indications = MagicMock(side_effect=RuntimeError("gatttool exited"))

        assert instance.read_status() == {}
        assert instance._connected is False
        assert instance.connected is False


class TestControlParity:
    @pytest.mark.parametrize(
        "reported_value,expected_result",
        [(False, True), (True, False)],
    )
    def test_default_control_write_uses_readback_result(self, reported_value, expected_result):
        controls = {"dc_switch_hm": {"id": 38, "type": "BOOL", "access": "RW"}}
        instance = make_transport(controls=controls)
        instance._send_control_packet = MagicMock(return_value=True)
        instance.read_status = MagicMock(return_value={"dc_switch_hm": reported_value})

        result = instance.send_control(38, False, "BOOL")

        assert result is expected_result
        instance._send_control_packet.assert_called_once_with(38, False, "BOOL")
        instance.read_status.assert_called_once_with()

    @pytest.mark.parametrize("packet_result", [True, False])
    def test_verify_false_returns_packet_result_without_readback(self, packet_result):
        controls = {"dc_switch_hm": {"id": 38, "type": "BOOL", "access": "RW"}}
        instance = make_transport(controls=controls)
        instance._send_control_packet = MagicMock(return_value=packet_result)
        instance.read_status = MagicMock()

        result = instance.send_control(38, False, "BOOL", verify=False)

        assert result is packet_result
        instance._send_control_packet.assert_called_once_with(38, False, "BOOL")
        instance.read_status.assert_not_called()


class TestCleanupAndFailureState:
    def test_handshake_write_failure_closes_gatttool_and_clears_session(self, monkeypatch):
        child = ScriptedGatttool([0, 0, FakeTimeout()])
        fake_pexpect = FakePexpectModule(child)
        monkeypatch.setattr(transport_module, "pexpect", fake_pexpect)
        reset_adapter = MagicMock()
        monkeypatch.setattr(transport_module.subprocess, "run", reset_adapter)
        instance = BLETransport(AUTH_KEY_B64, device_address=DEVICE_ADDRESS)

        assert instance.connect() is False

        reset_adapter.assert_called_once_with(
            ["hciconfig", "hci0", "reset"], capture_output=True, timeout=5
        )
        assert fake_pexpect.spawn_calls == [
            (
                "gatttool -b AA:BB:CC:DD:EE:FF -I",
                {"encoding": "utf-8", "timeout": 30},
            )
        ]
        assert child.sent == [
            "connect",
            "mtu 256",
            "char-write-req 0x0013 0200",
            "char-write-req 0x0012 aaaa0005a300017032",
            "disconnect",
            "exit",
        ]
        assert child.close_calls == [True]
        assert instance._gt is None
        assert instance._connected is False
        assert instance._encrypted is False
        assert instance._iv is None
        assert instance._iv_str is None
        assert instance.connected is False
