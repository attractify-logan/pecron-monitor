"""Bluetooth Low Energy transport for Pecron device communication."""

import base64
import hashlib
import logging
import re
import struct
import subprocess
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
# BLE Transport (gatttool-based)
# ===========================================================================

try:
    import pexpect

    HAS_BLE = True
except ImportError:
    HAS_BLE = False

BLE_WRITE_HANDLE = "0x0012"
BLE_CCCD_HANDLE = "0x0013"
BLE_DEVICE_PREFIX = "QUEC_BLE"


class BLETransport:
    """Bluetooth Low Energy transport for Pecron devices.

    Uses gatttool (interactive mode) via pexpect to bypass BlueZ D-Bus
    authorization restrictions that block bleak/bluepy GATT writes.

    Requires: pexpect (pip install pexpect), gatttool (part of bluez package)
    """

    def __init__(
        self,
        auth_key_b64: str,
        device_address: str = None,
        device_key: str = None,
        scan_timeout: float = 10.0,
        controls: dict = None,
    ):
        if not HAS_BLE:
            raise ImportError("pexpect is required for BLE transport: pip install pexpect")

        self.auth_key = base64.b64decode(auth_key_b64)
        self.auth_key_b64 = auth_key_b64
        self.device_address = device_address
        self.device_key = device_key
        self.scan_timeout = scan_timeout

        self._ble_suffix = device_key[-4:].upper() if device_key else None

        self._gt = None  # pexpect gatttool process
        self._iv = None  # AES IV (from handshake)
        self._iv_str = None  # Raw IV string
        self._encrypted = False
        self._packet_id = 0
        self._lock = threading.Lock()
        self._connected = False
        self._has_connected_once = False
        # Optional device controls mapping for id->code lookup
        self.controls = controls

    @property
    def connected(self) -> bool:
        return self._connected and self._encrypted

    def _next_pid(self) -> int:
        self._packet_id = (self._packet_id + 1) % 65535
        return self._packet_id

    def _collect_indications(self, wait: float = 3.0, extra_wait: float = 3.0) -> bytes:
        """Collect all BLE indication data from gatttool output."""
        time.sleep(wait)
        all_output = self._gt.before or ""
        try:
            while True:
                self._gt.expect(r"value:.*", timeout=extra_wait)
                all_output += (self._gt.before or "") + (self._gt.after or "")
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

        hex_data = ""
        for m in re.finditer(r"value:\s*([0-9a-f ]+)", all_output, re.I):
            hex_data += m.group(1).replace(" ", "")
        return bytes.fromhex(hex_data) if hex_data else b""

    def _parse_all_packets(self, raw: bytes) -> list:
        """Split concatenated indication data into individual TTLV packets."""
        packets = []
        i = 0
        while i < len(raw) - 4:
            if raw[i] == 0xAA and raw[i + 1] == 0xAA:
                pkt_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
                total = 4 + pkt_len
                if i + total <= len(raw):
                    packets.append(_ttlv_parse_packet(raw[i : i + total]))
                i += total
            else:
                i += 1
        return packets

    def _write_and_expect(self, hex_data: str, timeout: float = 5.0) -> bool:
        """Write to characteristic and expect 'successfully'."""
        self._gt.sendline(f"char-write-req {BLE_WRITE_HANDLE} {hex_data}")
        try:
            self._gt.expect("successfully", timeout=timeout)
            return True
        except (pexpect.TIMEOUT, pexpect.EOF):
            return False

    def _encrypt(self, data: bytes) -> bytes:
        cipher = AES.new(self.auth_key, AES.MODE_CBC, self._iv)
        return cipher.encrypt(pad(data, 16))

    def _decrypt(self, data: bytes) -> bytes:
        cipher = AES.new(self.auth_key, AES.MODE_CBC, self._iv)
        return unpad(cipher.decrypt(data), 16)

    def connect(self) -> bool:
        """Connect to Pecron device over BLE via gatttool."""
        if not self.device_address:
            log.error("BLE: device_address required for gatttool transport")
            return False

        try:
            # Reset HCI adapter to clear stale connections
            try:
                subprocess.run(["hciconfig", "hci0", "reset"], capture_output=True, timeout=5)
                time.sleep(1)
            except Exception:
                pass  # Non-fatal — adapter may still work

            # Start gatttool interactive
            self._gt = pexpect.spawn(
                f"gatttool -b {self.device_address} -I", encoding="utf-8", timeout=30
            )

            # Connect
            self._gt.sendline("connect")
            try:
                self._gt.expect("Connection successful", timeout=15)
            except pexpect.TIMEOUT:
                log.error("BLE: connection timeout for %s", self.device_address)
                self._cleanup()
                return False

            log.info("BLE connected to %s", self.device_address)
            self._connected = True
            time.sleep(0.5)

            # Request MTU 256 (allows 77-byte login in single write)
            self._gt.sendline("mtu 256")
            time.sleep(1)

            # Enable indications on CCCD
            self._gt.sendline(f"char-write-req {BLE_CCCD_HANDLE} 0200")
            try:
                self._gt.expect("successfully", timeout=5)
            except pexpect.TIMEOUT:
                log.warning("BLE: CCCD write timeout, continuing anyway")
            time.sleep(0.3)

            # Handshake: request random IV
            pkt = _ttlv_build_packet(0x7032, b"", self._next_pid())
            if not self._write_and_expect(pkt.hex()):
                log.error("BLE: random request write failed")
                self._cleanup()
                return False

            raw = self._collect_indications(wait=3, extra_wait=3)
            iv_str = self._extract_iv(raw)

            # Retry once if IV extraction failed (timing issue)
            if not iv_str or len(iv_str) < 16:
                log.debug("BLE: IV retry (got '%s')", iv_str)
                pkt = _ttlv_build_packet(0x7032, b"", self._next_pid())
                if not self._write_and_expect(pkt.hex()):
                    self._cleanup()
                    return False
                raw = self._collect_indications(wait=4, extra_wait=3)
                iv_str = self._extract_iv(raw)

            if not iv_str or len(iv_str) < 16:
                log.error("BLE: failed to get IV (got '%s')", iv_str)
                self._cleanup()
                return False

            self._iv_str = iv_str
            log.debug("BLE IV: %s", iv_str)

            # Login
            auth_hex = self.auth_key.hex()
            login_hash = hashlib.sha256(f"{auth_hex};{iv_str}".encode("utf-8")).hexdigest()
            login_payload = _ttlv_build_bytes_field(2, login_hash.encode("utf-8"))
            login_pkt = _ttlv_build_packet(0x7034, login_payload, self._next_pid())

            if not self._write_and_expect(login_pkt.hex()):
                log.error("BLE: login write failed")
                self._cleanup()
                return False

            raw = self._collect_indications(wait=3, extra_wait=3)
            parsed = _ttlv_parse_packet(raw)
            if parsed.get("cmd") != 0x7035:
                log.error("BLE: login failed (cmd=0x%04x)", parsed.get("cmd", 0))
                self._cleanup()
                return False

            # Set up encryption IV
            iv_bytes = iv_str.encode("utf-8")
            if len(iv_bytes) < 16:
                iv_bytes = iv_bytes.ljust(16, b"\x00")
            elif len(iv_bytes) > 16:
                iv_bytes = iv_bytes[:16]
            self._iv = iv_bytes
            self._encrypted = True
            self._has_connected_once = True

            log.info("BLE handshake complete — encryption active")
            return True

        except Exception as e:
            log.error("BLE connect error: %s", e)
            self._cleanup()
            return False

    def _extract_iv(self, raw: bytes) -> str:
        """Extract IV string from indication data."""
        if not raw:
            return None
        try:
            parsed = _ttlv_parse_packet(raw)
            fields = _ttlv_parse_fields(parsed.get("payload", b""))
            for fid, ftype, fval in fields:
                if fid == 1 and isinstance(fval, bytes):
                    return fval.decode("utf-8")
        except Exception as e:
            log.debug("BLE IV parse error: %s", e)
        return None

    def _cleanup(self):
        """Clean up gatttool process."""
        if self._gt:
            try:
                self._gt.sendline("disconnect")
                time.sleep(0.3)
                self._gt.sendline("exit")
                time.sleep(0.2)
            except Exception:
                pass
            try:
                self._gt.close(force=True)
            except Exception:
                pass
        self._gt = None
        self._connected = False
        self._encrypted = False
        self._iv = None
        self._iv_str = None

    def disconnect(self):
        """Disconnect from BLE device."""
        self._cleanup()

    def read_status(self) -> dict:
        """Read device status over BLE.

        Sends cmd 0x0011 and collects all response packets. Handles both
        encrypted (0x0012) and settings (0x0014) packets, merging fields
        from all packets into a single kv dict.
        """
        if not self.connected:
            return {}

        with self._lock:
            try:
                pkt = _ttlv_build_packet(0x0011, b"", self._next_pid())
                if not self._write_and_expect(pkt.hex()):
                    log.error("BLE: status read write failed")
                    self._connected = False
                    return {}

                # BLE responses arrive as indications over ~5-8 seconds
                raw = self._collect_indications(wait=5, extra_wait=5)
                if not raw:
                    # Retry once
                    log.debug("BLE: no status data, retrying...")
                    pkt = _ttlv_build_packet(0x0011, b"", self._next_pid())
                    if not self._write_and_expect(pkt.hex()):
                        self._connected = False
                        return {}
                    raw = self._collect_indications(wait=6, extra_wait=5)

                if not raw:
                    log.warning("BLE: no status data after retry")
                    return {}

                # Parse all TTLV packets and merge fields
                all_fields = []
                for parsed in self._parse_all_packets(raw):
                    cmd = parsed.get("cmd", 0)
                    payload = parsed.get("payload", b"")
                    if not payload or len(payload) < 16:
                        continue

                    try:
                        decrypted = self._decrypt(payload)
                        fields = _ttlv_parse_fields(decrypted)
                        all_fields.extend(fields)
                        log.debug("BLE packet cmd=0x%04x: %d fields", cmd, len(fields))
                    except Exception as e:
                        # Try as unencrypted (some cmd types)
                        try:
                            fields = _ttlv_parse_fields(payload)
                            if fields:
                                all_fields.extend(fields)
                        except Exception:
                            log.debug("BLE decrypt/parse failed: %s", e)

                if not all_fields:
                    log.warning("BLE: no parseable fields in response")
                    return {}

                log.debug("BLE: collected %d fields total", len(all_fields))
                return _fields_to_kv(all_fields, controls=self.controls)

            except Exception as e:
                log.error("BLE read failed: %s", e)
                self._connected = False
                return {}

    def send_control(
        self, data_point_id: int, value, ctrl_type: str = "BOOL", verify: bool = True
    ) -> bool:
        """Send a control command over BLE and (by default) verify it took effect.

        Mirrors the read-back-verification model used by `LocalTransport.send_control`
        (see issue #46). Returns True only when a post-write read confirms the
        data point now reflects the requested value.

        Pass `verify=False` for transient control codes that the device
        intentionally auto-reverts (see issue #50). With verification disabled
        this skips the post-write read-back and returns whatever
        `_send_control_packet` returned.
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
        """Build and send the TTLV write packet over BLE; collect response."""
        with self._lock:
            try:
                ctrl_type = ctrl_type.upper()
                if ctrl_type == "BOOL":
                    tag = (data_point_id << 3) | (1 if value else 0)
                    raw_payload = struct.pack(">H", tag)
                else:
                    tag = (data_point_id << 3) | 2
                    raw_payload = struct.pack(">H", tag) + bytes([int(value)])

                enc_payload = self._encrypt(raw_payload)
                pkt = _ttlv_build_packet(0x0013, enc_payload, self._next_pid())

                if not self._write_and_expect(pkt.hex()):
                    log.error("BLE: control write failed")
                    self._connected = False
                    return False

                # Collect response (0x7036 ack + 0x0014 confirmation)
                self._collect_indications(wait=2, extra_wait=2)
                log.info("BLE control write sent: field=%d value=%s", data_point_id, value)
                return True

            except Exception as e:
                log.error("BLE control failed: %s", e)
                self._connected = False
                return False

    def _verify_control_write(self, data_point_id: int, expected_value, ctrl_type: str) -> bool:
        """Read back device state via BLE and confirm data_point_id reflects expected_value."""
        field = self._field_for_data_point(data_point_id)
        if field is None:
            log.warning(
                "data_point_id=%d not in controls map for %s; cannot verify BLE write, "
                "falling back to best-effort success",
                data_point_id,
                getattr(self, "device_key", "?"),
            )
            return True

        time.sleep(0.5)

        try:
            kv = self.read_status()
        except Exception as e:
            log.warning("BLE read-back after write to %s failed: %s", field, e)
            return False

        if not kv:
            log.warning("BLE read-back after write to %s returned no fields", field)
            return False

        actual = kv.get(field)
        if actual is None:
            log.warning(
                "BLE read-back missing field %s; cannot confirm write of value=%r",
                field,
                expected_value,
            )
            return False

        if not _control_values_equal(expected_value, actual, ctrl_type):
            log.warning(
                "BLE write to %s not confirmed: requested=%r actual=%r",
                field,
                expected_value,
                actual,
            )
            return False

        log.debug("BLE write to %s confirmed by read-back: value=%r", field, actual)
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

    def is_alive(self) -> bool:
        """Check if the gatttool process is still running."""
        if not self._gt:
            return False
        return self._gt.isalive()


def scan_ble_devices(timeout: float = 10.0) -> list:
    """Scan for nearby Pecron BLE devices using hcitool.

    Returns list of (address, name) tuples.
    """
    results = []
    try:
        proc = subprocess.Popen(
            ["hcitool", "lescan", "--duplicates"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(timeout)
        proc.kill()
        stdout, _ = proc.communicate()
        seen = set()
        for line in stdout.decode("utf-8", errors="replace").split("\n"):
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                addr, name = parts
                if name.startswith(BLE_DEVICE_PREFIX) and addr not in seen:
                    results.append((addr, name))
                    seen.add(addr)
    except Exception as e:
        log.debug("BLE scan failed: %s", e)
    return results
