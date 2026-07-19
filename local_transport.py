"""TCP transport for local Pecron device communication.

Connects to Pecron devices on LAN over TCP/6607, performs the encrypted
handshake, and sends and receives local TTLV commands.
"""

import base64
import hashlib
import logging
import socket
import struct
import threading
import time

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from protocol import (
    _control_values_equal,
    _fields_to_kv,
    _ttlv_build_bytes_field,
    _ttlv_build_packet,
    _ttlv_parse_fields,
    _ttlv_parse_packet,
)

log = logging.getLogger("pecron")


# ===========================================================================
# LocalTransport
# ===========================================================================


class LocalTransport:
    """TCP transport for Pecron devices on LAN (port 6607)."""

    def __init__(
        self,
        device_ip: str,
        auth_key_b64: str,
        timeout: float = 10.0,
        device_key: str = None,
        controls: dict = None,
        multi_packet_timeout: float = 3.0,
    ):
        self.device_ip = device_ip
        self.device_port = 6607
        self.auth_key = base64.b64decode(auth_key_b64)
        self.auth_key_b64 = auth_key_b64
        self.timeout = timeout
        # Per-packet timeout while collecting a multi-packet read_status()
        # response. Some models (E3600/E3800) need longer gaps between
        # packets than others — see LOCAL_READ_TIMEOUT_OVERRIDES (issue #84).
        self.multi_packet_timeout = multi_packet_timeout

        self._sock = None
        self._iv = None  # Set after handshake
        self._encrypted = False
        self._packet_id = 0
        self._lock = threading.Lock()
        self._connected = False
        self._has_connected_once = False
        # Optional device-scoped controls mapping (code->info) used for id->code lookup
        self.device_key = device_key
        self.controls = controls

    @property
    def connected(self) -> bool:
        return self._connected and self._encrypted

    def _next_pid(self) -> int:
        self._packet_id = (self._packet_id + 1) % 65535
        return self._packet_id

    def connect(self) -> bool:
        """Perform TCP connect + WiFi handshake (random exchange + login)."""
        try:
            self.disconnect()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.device_ip, self.device_port))
            self._connected = True
            if not self._has_connected_once:
                log.info("Local TCP connected to %s:%d", self.device_ip, self.device_port)
            else:
                log.debug("Local TCP reconnected to %s:%d", self.device_ip, self.device_port)

            # Step 1: Request random (IV)
            pkt = _ttlv_build_packet(0x7032, b"", self._next_pid())
            self._sock.sendall(pkt)

            resp = self._recv_packet()
            parsed = _ttlv_parse_packet(resp)
            if parsed.get("cmd") != 0x7033:
                log.error("Expected cmd 0x7033, got 0x%04x", parsed.get("cmd", 0))
                self.disconnect()
                return False

            # Extract random string from TTLV field id=1
            fields = _ttlv_parse_fields(parsed["payload"])
            random_str = None
            for fid, ftype, fval in fields:
                if fid == 1 and isinstance(fval, bytes):
                    random_str = fval.decode("utf-8")
                    break
            if not random_str:
                log.error("No random/IV in 0x7033 response")
                self.disconnect()
                return False

            log.debug("Got random/IV: %s", random_str)

            # Step 2: Login with SHA-256 hash
            auth_hex = self.auth_key.hex()
            login_hash = hashlib.sha256(f"{auth_hex};{random_str}".encode("utf-8")).hexdigest()
            login_payload = _ttlv_build_bytes_field(2, login_hash.encode("utf-8"))
            pkt = _ttlv_build_packet(0x7034, login_payload, self._next_pid())
            self._sock.sendall(pkt)

            resp = self._recv_packet()
            parsed = _ttlv_parse_packet(resp)
            if parsed.get("cmd") != 0x7035:
                log.error("Login failed — expected 0x7035, got 0x%04x", parsed.get("cmd", 0))
                self.disconnect()
                return False

            # Check login result (field id=3, value=0 means success)
            fields = _ttlv_parse_fields(parsed["payload"])
            for fid, ftype, fval in fields:
                if ftype == "NUM" and fval != 0:
                    log.error("Login rejected (result=%s)", fval)
                    self.disconnect()
                    return False

            # Set up encryption
            iv_bytes = random_str.encode("utf-8")
            if len(iv_bytes) < 16:
                iv_bytes = iv_bytes.ljust(16, b"\x00")
            elif len(iv_bytes) > 16:
                iv_bytes = iv_bytes[:16]
            self._iv = iv_bytes
            self._encrypted = True
            if not self._has_connected_once:
                log.info("Local TCP handshake complete — encryption active")
                self._has_connected_once = True
            else:
                log.debug("Local TCP handshake complete")
            return True

        except Exception as e:
            # Pecron devices close TCP after each read — reconnects are normal
            log.debug("Local connect failed: %s", e)
            self.disconnect()
            return False

    def disconnect(self):
        self._connected = False
        self._encrypted = False
        self._iv = None
        # Reset read flags for next connection
        if hasattr(self, "_first_read_done"):
            delattr(self, "_first_read_done")
        if hasattr(self, "_retried_read"):
            delattr(self, "_retried_read")
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _recv_packet(self) -> bytes:
        """Read one TTLV packet from socket."""
        buf = b""
        # Sync to 0xAA 0xAA
        while True:
            b = self._sock.recv(1)
            if not b:
                raise ConnectionError("Connection closed")
            buf += b
            if len(buf) >= 2 and buf[-2:] == b"\xaa\xaa":
                buf = b"\xaa\xaa"
                break
            if len(buf) > 200:
                raise ValueError("No sync found")

        # Read length (2 bytes) — careful with byte stuffing
        len_raw = b""
        while len(len_raw) < 2:
            b = self._sock.recv(1)
            if not b:
                raise ConnectionError("Connection closed")
            buf += b
            if buf[-2] == 0xAA and b[0] == 0x55:
                continue
            len_raw += b

        pkt_len = struct.unpack(">H", len_raw)[0]
        remaining = pkt_len
        while remaining > 0:
            chunk = self._sock.recv(min(remaining, 4096))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf += chunk
            remaining -= len(chunk)

        return buf

    def _decrypt(self, data: bytes) -> bytes:
        cipher = AES.new(self.auth_key, AES.MODE_CBC, self._iv)
        return unpad(cipher.decrypt(data), 16)

    def _encrypt(self, data: bytes) -> bytes:
        cipher = AES.new(self.auth_key, AES.MODE_CBC, self._iv)
        return cipher.encrypt(pad(data, 16))

    def read_status(self) -> dict:
        """Send read command and return kv dict matching MQTT format.

        Some devices (E3800, E3600) send data split across multiple 0x0014 packets.
        We collect all packets and merge their fields to get complete data.

        E3800LFP firmware quirk: Device needs a brief pause after handshake before
        accepting read commands. If first read returns 0 fields, wait and retry once.
        """
        if not self.connected:
            return {}

        with self._lock:
            try:
                # E3800LFP quirk: Add delay after handshake to prevent connection drop
                # (some firmware versions close the socket if read comes too soon)
                if not hasattr(self, "_first_read_done"):
                    time.sleep(0.5)
                    self._first_read_done = True

                # Send cmd 0x0011 (read)
                pkt = _ttlv_build_packet(0x0011, b"", self._next_pid())
                self._sock.sendall(pkt)

                # Collect all response packets (some devices send 3-4 packets)
                all_fields = []
                packets_read = 0
                max_packets = 10  # Safety limit

                # Temporarily reduce socket timeout for multi-packet reads.
                # Per-model: E3800/E3600 can take 3-4 seconds between packets
                # (see self.multi_packet_timeout / LOCAL_READ_TIMEOUT_OVERRIDES).
                original_timeout = self._sock.gettimeout()
                self._sock.settimeout(self.multi_packet_timeout)

                while packets_read < max_packets:
                    try:
                        resp = self._recv_packet()
                        parsed = _ttlv_parse_packet(resp)
                        cmd = parsed.get("cmd", 0)

                        # Skip ACK packets (0x0012)
                        if cmd == 0x0012:
                            packets_read += 1
                            continue

                        # Process data packets (0x0014)
                        if cmd == 0x0014:
                            payload = parsed.get("payload", b"")
                            if payload:
                                decrypted = self._decrypt(payload)
                                fields = _ttlv_parse_fields(decrypted)
                                all_fields.extend(fields)
                                packets_read += 1
                                log.debug(
                                    "Read packet %d with %d fields", packets_read, len(fields)
                                )
                        else:
                            # Unknown command, stop reading
                            break

                    except socket.timeout:
                        # No more packets available
                        break
                    except Exception as e:
                        log.debug("Packet read error: %s", e)
                        break

                # Restore original timeout
                self._sock.settimeout(original_timeout)

                if not all_fields:
                    # E3800 quirk: Sometimes device needs time to prepare data after handshake
                    # Retry once with a longer delay
                    if not hasattr(self, "_retried_read"):
                        log.debug("No data fields in first read, retrying in 1s...")
                        self._retried_read = True
                        time.sleep(1.0)
                        # Retry read command
                        pkt = _ttlv_build_packet(0x0011, b"", self._next_pid())
                        self._sock.sendall(pkt)
                        self._sock.settimeout(self.multi_packet_timeout)
                        all_fields = []
                        packets_read = 0
                        while packets_read < max_packets:
                            try:
                                resp = self._recv_packet()
                                parsed = _ttlv_parse_packet(resp)
                                cmd = parsed.get("cmd", 0)
                                if cmd == 0x0012:
                                    packets_read += 1
                                    continue
                                if cmd == 0x0014:
                                    payload = parsed.get("payload", b"")
                                    if payload:
                                        decrypted = self._decrypt(payload)
                                        fields = _ttlv_parse_fields(decrypted)
                                        all_fields.extend(fields)
                                        packets_read += 1
                                        log.debug(
                                            "Retry: read packet %d with %d fields",
                                            packets_read,
                                            len(fields),
                                        )
                                else:
                                    break
                            except socket.timeout:
                                break
                            except Exception as e:
                                log.debug("Retry packet read error: %s", e)
                                break
                        self._sock.settimeout(original_timeout)

                    if not all_fields:
                        log.warning("No data fields in local read response (even after retry)")
                        return {}

                log.debug(
                    "Collected %d total fields from %d packets", len(all_fields), packets_read
                )
                kv = _fields_to_kv(all_fields, controls=self.controls)
                return kv

            except Exception as e:
                log.debug("Local read ended: %s", e)
                self._connected = False
                return {}

    def send_control(
        self, data_point_id: int, value, ctrl_type: str = "BOOL", verify: bool = True
    ) -> bool:
        """Send a control command over local TCP and (by default) verify it took effect.

        Returns True only when a post-write read-back confirms the data point
        now reflects the requested value. False indicates the write was sent
        but the device did not apply it (rejected, watchdog-reset, malformed
        response, etc.) -- see issue #46. When the controls TSL doesn't include
        the data point, falls back to "best-effort True" with a warning since
        we have no field name to read back against.

        Pass `verify=False` for transient control codes that the device
        intentionally auto-reverts (e.g. `high_frequency_reporting`, see
        issue #50). With verification disabled this skips the post-write
        read-back and returns whatever `_send_control_packet` returned.
        """
        if not self.connected:
            return False

        sent = self._send_control_packet(data_point_id, value, ctrl_type)
        if not sent:
            return False

        if not verify:
            return sent

        return self._verify_control_write(data_point_id, value, ctrl_type)

    def _send_control_packet(self, data_point_id: int, value, ctrl_type: str) -> bool:
        """Build and send the TTLV write packet. Returns True if the packet
        went out and a response was received at the transport level. Does NOT
        verify the device accepted the write — callers must read back."""
        with self._lock:
            try:
                ctrl_type = ctrl_type.upper()
                if ctrl_type == "BOOL":
                    tag = (data_point_id << 3) | (1 if value else 0)
                    raw_payload = struct.pack(">H", tag)
                else:
                    tag = (data_point_id << 3) | 2
                    raw_payload = struct.pack(">H", tag) + bytes([int(value)])

                log.debug("Raw payload: %s (tag=0x%04x)", raw_payload.hex(), tag)
                enc_payload = self._encrypt(raw_payload)
                pkt = _ttlv_build_packet(0x0013, enc_payload, self._next_pid())
                self._sock.sendall(pkt)

                resp = self._recv_packet()
                parsed = _ttlv_parse_packet(resp)
                log.info("Local control write response: cmd=0x%04x", parsed.get("cmd", 0))
                return True

            except Exception as e:
                log.error("Local control send failed: %s", e)
                self._connected = False
                return False

    def _verify_control_write(self, data_point_id: int, expected_value, ctrl_type: str) -> bool:
        """Read back device state and confirm data_point_id reflects expected_value.

        Returns True only when the read-back confirms the write took effect.
        Falls back to best-effort True (with warning) if the controls TSL has
        no entry for data_point_id — without a field name we can't index into
        the read response.
        """
        field = self._field_for_data_point(data_point_id)
        if field is None:
            log.warning(
                "data_point_id=%d not in controls map for %s; cannot verify write, "
                "falling back to best-effort success",
                data_point_id,
                self.device_key,
            )
            return True

        # Brief settling delay; some firmwares need a moment before reads
        # reflect a just-written value.
        time.sleep(0.5)

        try:
            kv = self.read_status()
        except Exception as e:
            log.warning("Read-back after write to %s failed: %s", field, e)
            return False

        if not kv:
            log.warning("Read-back after write to %s returned no fields", field)
            return False

        actual = kv.get(field)
        if actual is None:
            log.warning(
                "Read-back missing field %s; cannot confirm write of value=%r",
                field,
                expected_value,
            )
            return False

        if not _control_values_equal(expected_value, actual, ctrl_type):
            log.warning(
                "Write to %s not confirmed: requested=%r actual=%r; device may have "
                "rejected the write or watchdog-reset (see issue #46)",
                field,
                expected_value,
                actual,
            )
            return False

        log.debug("Write to %s confirmed by read-back: value=%r", field, actual)
        return True

    def _field_for_data_point(self, data_point_id: int):
        """Reverse-lookup the TSL code for a data_point_id from self.controls."""
        if not self.controls:
            return None
        for code, info in self.controls.items():
            try:
                if info.get("id") == data_point_id:
                    return code
            except AttributeError:
                continue
        return None
