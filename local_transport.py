"""
Local transports for Pecron Monitor — TCP/6607 and BLE with AES-CBC encryption.

Connects to Pecron device on LAN (WiFi TCP) or via Bluetooth Low Energy,
performs handshake (random exchange + SHA-256 login), then sends/receives
encrypted TTLV commands. Produces the same kv dict structure as MQTT
so existing _process_data() works unchanged.

This module is optional — pecron_monitor.py works without it (cloud-only mode).

Requires: pycryptodome (pip install pycryptodome)
Optional: pexpect (pip install pexpect) + gatttool (bluez) — for BLE transport
"""

import base64
import hashlib
import logging
import socket
import struct
import re
import subprocess
import threading
import time
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

log = logging.getLogger("pecron")


# ===========================================================================
# TTLV codec (local TCP variant — AES-CBC encrypted payloads)
# ===========================================================================

def _ttlv_crc(data: bytes) -> int:
    return sum(data) & 0xFF


def _ttlv_byte_stuff(raw: bytes) -> bytes:
    out = bytearray(raw[:2])
    i = 2
    while i < len(raw):
        out.append(raw[i])
        if i < len(raw) - 1 and raw[i] == 0xAA and raw[i + 1] in (0x55, 0xAA):
            out.append(0x55)
        i += 1
    return bytes(out)


def _ttlv_byte_unstuff(raw: bytes) -> bytes:
    out = bytearray(raw[:2])
    i = 2
    while i < len(raw):
        if i < len(raw) - 1 and raw[i] == 0xAA and raw[i + 1] == 0x55:
            out.append(0xAA)
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def _ttlv_build_packet(cmd: int, payload: bytes = b"", packet_id: int = 1) -> bytes:
    inner = struct.pack(">HH", packet_id, cmd) + payload
    crc = _ttlv_crc(inner)
    length = len(inner) + 1
    return _ttlv_byte_stuff(
        b"\xaa\xaa" + struct.pack(">H", length) + bytes([crc]) + inner
    )


def _ttlv_build_bytes_field(tag_id: int, data: bytes) -> bytes:
    tag_word = ((tag_id << 3) & 0xFFF8) | 3
    return struct.pack(">H", tag_word) + struct.pack(">H", len(data)) + data


def _ttlv_parse_packet(data: bytes) -> dict:
    data = _ttlv_byte_unstuff(data)
    if len(data) < 9 or data[0] != 0xAA or data[1] != 0xAA:
        return {"error": "bad packet", "raw": data.hex()}
    pkt_len = struct.unpack(">H", data[2:4])[0]
    pid = struct.unpack(">H", data[5:7])[0]
    cmd = struct.unpack(">H", data[7:9])[0]
    payload = data[9:4 + pkt_len] if len(data) >= 4 + pkt_len else data[9:]
    return {"cmd": cmd, "packet_id": pid, "payload": payload}


def _ttlv_parse_fields(payload: bytes) -> list:
    """Parse TTLV fields from decrypted payload. Returns list of (id, type, value)."""
    fields = []
    i = 0
    while i < len(payload) - 1:
        tag_word = struct.unpack(">H", payload[i:i + 2])[0]
        tag_id = (tag_word >> 3) & 0x1FFF
        tag_type = tag_word & 0x07
        i += 2

        if tag_type in (0, 1):  # Boolean
            fields.append((tag_id, "BOOL", tag_type == 1))
        elif tag_type == 2:  # Number
            if i >= len(payload):
                break
            meta = payload[i]
            i += 1
            sign = (meta >> 7) & 1
            decimals = (meta >> 3) & 0x0F
            byte_count = (meta & 0x07) + 1
            if i + byte_count > len(payload):
                break
            val = int.from_bytes(payload[i:i + byte_count], "big")
            i += byte_count
            if sign:
                val = -val
            if decimals > 0:
                val = val / (10 ** decimals)
            fields.append((tag_id, "NUM", val))
        elif tag_type in (3, 5):  # Bytes
            if i + 2 > len(payload):
                break
            dlen = struct.unpack(">H", payload[i:i + 2])[0]
            i += 2
            fields.append((tag_id, "BYTES", payload[i:i + dlen]))
            i += dlen
        elif tag_type == 4:  # Struct/Array header
            if i + 2 > len(payload):
                break
            count = struct.unpack(">H", payload[i:i + 2])[0]
            i += 2
            fields.append((tag_id, "STRUCT", count))
        else:
            break

    return fields


# ===========================================================================
# TSL ID → kv dict translation
# Maps local TTLV numeric IDs back to the same nested dict keys that the
# cloud MQTT path uses, so _process_data() works unchanged.
# ===========================================================================

# Top-level property ID → TSL code
TSL_TOP = {
    1: "battery_percentage",
    2: "remain_time",
    3: "remain_charging_time",
    4: "total_input_power",
    5: "total_output_power",
    27: "ups_status_hm",
    28: "dc_data_input_hm",
    29: "ac_data_input_hm",
    30: "dc_data_output_hm",
    31: "ac_data_output_hm",
    32: "ac_output_voltage_io",
    33: "ac_output_frequency_io",
    34: "noastime_io",
    35: "host_packet_data_jdb",
    36: "charging_pack_data_jdb",
    37: "device_status_hm",
    38: "dc_switch_hm",
    39: "add_bat_status_hm",
    40: "ac_switch_hm",
    41: "device_mode_info",  # E3800: mode status
    42: "device_touch_locking_as",  # E3800: touch panel lock
    43: "auto_light_flag_as",
    44: "eco_quite_mode_as",  # E3800: eco/quiet mode
    45: "machine_screen_light_as",
    46: "ups_start_charge_value_as",  # E3800: UPS charge threshold
    47: "battery_temp",  # E3800: battery temperature
    48: "charging_plate_temp",  # E3800: charging plate temperature
    49: "inverter_temp",  # E3800: inverter temperature
    50: "ac_charging_power_ios",  # E3800: AC charging power level
    51: "device_standy_times_as",  # E3800/WB12200: standby timeout
    52: "device_manual",
    55: "dc_charging_power_enable",  # E3800: DC charging enable
    56: "bypass_enable",  # E3800: bypass enable
    # WB12200 battery management fields
    86: "battery_coding_us",
    87: "beep_voice_us",
    89: "battery_indicator_us",
    90: "FAULT_ALARM_ENUM",
    91: "battery_heating_mode",
    92: "charging_limit_voltage",
    93: "discharge_limiting_voltage",
    94: "charging_current_limit",
    95: "discharge_limiting_current",
    100: "high_frequency_reporting",
}

# Struct sub-field mappings: parent_code → {sub_id: sub_code}
TSL_STRUCT = {
    "host_packet_data_jdb": {
        1: "host_packet_electric_percentage",
        2: "host_packet_voltage",
        3: "host_packet_current",
        4: "host_packet_temp",
        5: "host_packet_status",
    },
    "ac_data_output_hm": {
        1: "ac_output_hz",
        2: "ac_output_voltage",
        3: "ac_output_pf",
        4: "ac_output_power",
    },
    "dc_data_output_hm": {
        1: "dc_output_power",
    },
    "ac_data_input_hm": {
        1: "ac_power",
    },
    "dc_data_input_hm": {
        1: "dc_input_power",
    },
    "charging_pack_data_jdb": {
        # Array element struct fields
        1: "charging_pack_num",
        2: "charging_pack_battery",
        3: "charging_pack_voltage",
        4: "charging_pack_current",
        5: "charging_pack_temp",
        6: "charging_pack_status",
    },
}

# SENSOR_FIELDS expects these nested paths for the cloud MQTT format:
#   battery_percent → ("host_packet_data_jdb", "host_packet_electric_percentage")
#   voltage → ("host_packet_data_jdb", "host_packet_voltage")
# etc. So we rebuild that same nested dict structure.


def _fields_to_kv(fields: list, controls: dict = None) -> dict:
    """Convert parsed TTLV fields into the nested kv dict matching MQTT format."""
    kv = {}
    i = 0
    # Build id -> code mapping from provided controls when available
    id_to_code = {}
    if controls:
        try:
            for code, info in controls.items():
                cid = info.get("id")
                if isinstance(cid, int):
                    id_to_code[cid] = code
        except Exception:
            id_to_code = {}

    while i < len(fields):
        fid, ftype, fval = fields[i]
        # Prefer device-specific controls mapping, fall back to TSL_TOP
        code = id_to_code.get(fid, TSL_TOP.get(fid))

        if code is None:
            i += 1
            continue

        if ftype == "STRUCT":
            # fval is the count of sub-fields
            sub_map = TSL_STRUCT.get(code, {})
            sub_dict = {}
            count = fval
            j = i + 1
            consumed = 0
            is_array = False  # Track if this struct was handled as an array

            while j < len(fields) and consumed < count:
                sid, stype, sval = fields[j]
                sub_code = sub_map.get(sid, f"field_{sid}")
                if stype == "STRUCT":
                    # Nested struct (e.g., array elements in charging_pack)
                    # For arrays, collect into a list
                    if code == "charging_pack_data_jdb":
                        # Array of pack structs
                        packs = kv.get(code, [])
                        pack = {}
                        elem_count = sval
                        k = j + 1
                        ec = 0
                        while k < len(fields) and ec < elem_count:
                            eid, etype, eval_ = fields[k]
                            elem_code = sub_map.get(eid, f"field_{eid}")
                            if etype not in ("STRUCT",):
                                pack[elem_code] = eval_
                                ec += 1
                            k += 1
                        packs.append(pack)
                        kv[code] = packs
                        is_array = True
                        j = k
                        consumed += 1
                        continue
                    j += 1
                    consumed += 1
                    continue
                sub_dict[sub_code] = sval
                j += 1
                consumed += 1

            # Only set sub_dict if this wasn't an array (which already set kv[code])
            # Don't overwrite existing array data from earlier packets with dict format
            if not is_array and not isinstance(kv.get(code), list):
                kv[code] = sub_dict
            i = j
        elif ftype == "BOOL":
            kv[code] = fval
            i += 1
        elif ftype == "NUM":
            kv[code] = fval
            i += 1
        elif ftype == "BYTES":
            try:
                kv[code] = fval.decode("utf-8")
            except Exception:
                kv[code] = fval.hex()
            i += 1
        else:
            kv[code] = fval
            i += 1

    return kv


# ===========================================================================
# LocalTransport
# ===========================================================================

class LocalTransport:
    """TCP transport for Pecron devices on LAN (port 6607)."""
    def __init__(self, device_ip: str, auth_key_b64: str, timeout: float = 10.0,
                 device_key: str = None, controls: dict = None):
        self.device_ip = device_ip
        self.device_port = 6607
        self.auth_key = base64.b64decode(auth_key_b64)
        self.auth_key_b64 = auth_key_b64
        self.timeout = timeout

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
            login_hash = hashlib.sha256(
                f"{auth_hex};{random_str}".encode("utf-8")
            ).hexdigest()
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
        if hasattr(self, '_first_read_done'):
            delattr(self, '_first_read_done')
        if hasattr(self, '_retried_read'):
            delattr(self, '_retried_read')
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
                if not hasattr(self, '_first_read_done'):
                    time.sleep(0.5)
                    self._first_read_done = True

                # Send cmd 0x0011 (read)
                pkt = _ttlv_build_packet(0x0011, b"", self._next_pid())
                self._sock.sendall(pkt)

                # Collect all response packets (some devices send 3-4 packets)
                all_fields = []
                packets_read = 0
                max_packets = 10  # Safety limit

                # Temporarily reduce socket timeout for multi-packet reads
                # E3800/E3600 can take 2-3 seconds between packets
                original_timeout = self._sock.gettimeout()
                self._sock.settimeout(3.0)

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
                                log.debug("Read packet %d with %d fields", packets_read, len(fields))
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
                    if not hasattr(self, '_retried_read'):
                        log.debug("No data fields in first read, retrying in 1s...")
                        self._retried_read = True
                        time.sleep(1.0)
                        # Retry read command
                        pkt = _ttlv_build_packet(0x0011, b"", self._next_pid())
                        self._sock.sendall(pkt)
                        self._sock.settimeout(3.0)
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
                                        log.debug("Retry: read packet %d with %d fields", packets_read, len(fields))
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

                log.debug("Collected %d total fields from %d packets", len(all_fields), packets_read)
                kv = _fields_to_kv(all_fields, controls=self.controls)
                return kv

            except Exception as e:
                log.debug("Local read ended: %s", e)
                self._connected = False
                return {}

    def send_control(self, data_point_id: int, value, ctrl_type: str = "BOOL") -> bool:
        """Send a control command over local TCP and verify it took effect.

        Returns True only when a post-write read-back confirms the data point
        now reflects the requested value. False indicates the write was sent
        but the device did not apply it (rejected, watchdog-reset, malformed
        response, etc.) — see issue #46. When the controls TSL doesn't include
        the data point, falls back to "best-effort True" with a warning since
        we have no field name to read back against.
        """
        if not self.connected:
            return False

        if not self._send_control_packet(data_point_id, value, ctrl_type):
            return False

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
                data_point_id, self.device_key,
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
                field, expected_value,
            )
            return False

        if not _control_values_equal(expected_value, actual, ctrl_type):
            log.warning(
                "Write to %s not confirmed: requested=%r actual=%r; device may have "
                "rejected the write or watchdog-reset (see issue #46)",
                field, expected_value, actual,
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


def _control_values_equal(expected, actual, ctrl_type: str) -> bool:
    """Compare a requested control value against what the device reported back.

    BOOL fields come back from `_ttlv_parse_fields` as Python bool; ENUM/INT
    come back as int (or float when scaled). Normalize both sides before
    comparing so we don't trip on True vs 1, "1" vs 1, etc.
    """
    try:
        if ctrl_type.upper() == "BOOL":
            return bool(expected) == bool(actual)
        return int(actual) == int(expected)
    except (TypeError, ValueError):
        return False


# ===========================================================================
# BLE Transport (gatttool-based)
# ===========================================================================

try:
    import pexpect
    HAS_BLE = True
except ImportError:
    HAS_BLE = False

BLE_CHAR_UUID = "00009c40-0000-1000-8000-00805f9b34fb"
BLE_WRITE_HANDLE = "0x0012"
BLE_CCCD_HANDLE = "0x0013"
BLE_DEVICE_PREFIX = "QUEC_BLE"


class BLETransport:
    """Bluetooth Low Energy transport for Pecron devices.

    Uses gatttool (interactive mode) via pexpect to bypass BlueZ D-Bus
    authorization restrictions that block bleak/bluepy GATT writes.

    Requires: pexpect (pip install pexpect), gatttool (part of bluez package)
    """

    def __init__(self, auth_key_b64: str, device_address: str = None,
                 device_key: str = None, scan_timeout: float = 10.0,
                 controls: dict = None):
        if not HAS_BLE:
            raise ImportError("pexpect is required for BLE transport: pip install pexpect")

        self.auth_key = base64.b64decode(auth_key_b64)
        self.auth_key_b64 = auth_key_b64
        self.device_address = device_address
        self.device_key = device_key
        self.scan_timeout = scan_timeout

        self._ble_suffix = device_key[-4:].upper() if device_key else None

        self._gt = None          # pexpect gatttool process
        self._iv = None          # AES IV (from handshake)
        self._iv_str = None      # Raw IV string
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
        all_output = self._gt.before or ''
        try:
            while True:
                self._gt.expect(r'value:.*', timeout=extra_wait)
                all_output += (self._gt.before or '') + (self._gt.after or '')
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

        hex_data = ""
        for m in re.finditer(r'value:\s*([0-9a-f ]+)', all_output, re.I):
            hex_data += m.group(1).replace(' ', '')
        return bytes.fromhex(hex_data) if hex_data else b''

    def _parse_all_packets(self, raw: bytes) -> list:
        """Split concatenated indication data into individual TTLV packets."""
        packets = []
        i = 0
        while i < len(raw) - 4:
            if raw[i] == 0xAA and raw[i + 1] == 0xAA:
                pkt_len = struct.unpack('>H', raw[i + 2:i + 4])[0]
                total = 4 + pkt_len
                if i + total <= len(raw):
                    packets.append(_ttlv_parse_packet(raw[i:i + total]))
                i += total
            else:
                i += 1
        return packets

    def _write_and_expect(self, hex_data: str, timeout: float = 5.0) -> bool:
        """Write to characteristic and expect 'successfully'."""
        self._gt.sendline(f'char-write-req {BLE_WRITE_HANDLE} {hex_data}')
        try:
            self._gt.expect('successfully', timeout=timeout)
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
                subprocess.run(
                    ['hciconfig', 'hci0', 'reset'],
                    capture_output=True, timeout=5
                )
                time.sleep(1)
            except Exception:
                pass  # Non-fatal — adapter may still work

            # Start gatttool interactive
            self._gt = pexpect.spawn(
                f'gatttool -b {self.device_address} -I',
                encoding='utf-8', timeout=30
            )

            # Connect
            self._gt.sendline('connect')
            try:
                self._gt.expect('Connection successful', timeout=15)
            except pexpect.TIMEOUT:
                log.error("BLE: connection timeout for %s", self.device_address)
                self._cleanup()
                return False

            log.info("BLE connected to %s", self.device_address)
            self._connected = True
            time.sleep(0.5)

            # Request MTU 256 (allows 77-byte login in single write)
            self._gt.sendline('mtu 256')
            time.sleep(1)

            # Enable indications on CCCD
            self._gt.sendline(f'char-write-req {BLE_CCCD_HANDLE} 0200')
            try:
                self._gt.expect('successfully', timeout=5)
            except pexpect.TIMEOUT:
                log.warning("BLE: CCCD write timeout, continuing anyway")
            time.sleep(0.3)

            # Handshake: request random IV
            pkt = _ttlv_build_packet(0x7032, b'', self._next_pid())
            if not self._write_and_expect(pkt.hex()):
                log.error("BLE: random request write failed")
                self._cleanup()
                return False

            raw = self._collect_indications(wait=3, extra_wait=3)
            iv_str = self._extract_iv(raw)

            # Retry once if IV extraction failed (timing issue)
            if not iv_str or len(iv_str) < 16:
                log.debug("BLE: IV retry (got '%s')", iv_str)
                pkt = _ttlv_build_packet(0x7032, b'', self._next_pid())
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
            login_hash = hashlib.sha256(
                f"{auth_hex};{iv_str}".encode('utf-8')
            ).hexdigest()
            login_payload = _ttlv_build_bytes_field(2, login_hash.encode('utf-8'))
            login_pkt = _ttlv_build_packet(0x7034, login_payload, self._next_pid())

            if not self._write_and_expect(login_pkt.hex()):
                log.error("BLE: login write failed")
                self._cleanup()
                return False

            raw = self._collect_indications(wait=3, extra_wait=3)
            parsed = _ttlv_parse_packet(raw)
            if parsed.get('cmd') != 0x7035:
                log.error("BLE: login failed (cmd=0x%04x)", parsed.get('cmd', 0))
                self._cleanup()
                return False

            # Set up encryption IV
            iv_bytes = iv_str.encode('utf-8')
            if len(iv_bytes) < 16:
                iv_bytes = iv_bytes.ljust(16, b'\x00')
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
            fields = _ttlv_parse_fields(parsed.get('payload', b''))
            for fid, ftype, fval in fields:
                if fid == 1 and isinstance(fval, bytes):
                    return fval.decode('utf-8')
        except Exception as e:
            log.debug("BLE IV parse error: %s", e)
        return None

    def _cleanup(self):
        """Clean up gatttool process."""
        if self._gt:
            try:
                self._gt.sendline('disconnect')
                time.sleep(0.3)
                self._gt.sendline('exit')
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
                pkt = _ttlv_build_packet(0x0011, b'', self._next_pid())
                if not self._write_and_expect(pkt.hex()):
                    log.error("BLE: status read write failed")
                    self._connected = False
                    return {}

                # BLE responses arrive as indications over ~5-8 seconds
                raw = self._collect_indications(wait=5, extra_wait=5)
                if not raw:
                    # Retry once
                    log.debug("BLE: no status data, retrying...")
                    pkt = _ttlv_build_packet(0x0011, b'', self._next_pid())
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
                    cmd = parsed.get('cmd', 0)
                    payload = parsed.get('payload', b'')
                    if not payload or len(payload) < 16:
                        continue

                    try:
                        decrypted = self._decrypt(payload)
                        fields = _ttlv_parse_fields(decrypted)
                        all_fields.extend(fields)
                        log.debug("BLE packet cmd=0x%04x: %d fields",
                                  cmd, len(fields))
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

    def send_control(self, data_point_id: int, value, ctrl_type: str = "BOOL") -> bool:
        """Send a control command over BLE and verify it took effect.

        Mirrors the read-back-verification model used by `LocalTransport.send_control`
        (see issue #46). Returns True only when a post-write read confirms the
        data point now reflects the requested value.
        """
        if not self.connected:
            return False

        if not self._send_control_packet(data_point_id, value, ctrl_type):
            return False

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
                data_point_id, getattr(self, "device_key", "?"),
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
                field, expected_value,
            )
            return False

        if not _control_values_equal(expected_value, actual, ctrl_type):
            log.warning(
                "BLE write to %s not confirmed: requested=%r actual=%r",
                field, expected_value, actual,
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
            ['hcitool', 'lescan', '--duplicates'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(timeout)
        proc.kill()
        stdout, _ = proc.communicate()
        seen = set()
        for line in stdout.decode('utf-8', errors='replace').split('\n'):
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                addr, name = parts
                if name.startswith(BLE_DEVICE_PREFIX) and addr not in seen:
                    results.append((addr, name))
                    seen.add(addr)
    except Exception as e:
        log.debug("BLE scan failed: %s", e)
    return results


def get_auth_key(token: str, region: dict, pk: str, dk: str) -> str:
    """Fetch the device authKey from Quectel cloud (one-time, can be cached).
    
    Tries read-only getAuthKey first, then regenerateAuthKey as fallback.
    Some device models/accounts only support one or the other.
    """
    import urllib.parse
    import urllib.request
    import json

    last_error = None
    for endpoint in ["getAuthKey", "regenerateAuthKey"]:
        url = region["base_url"] + f"/v2/binding/enduserapi/{endpoint}"
        data = urllib.parse.urlencode({"pk": pk, "dk": dk}).encode()
        req = urllib.request.Request(url, data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Authorization", token)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
            if body.get("code") == 200:
                log.debug("Got authKey via %s", endpoint)
                return body["data"]["authKey"]
            last_error = body.get("msg", body)
            log.debug("%s failed: %s", endpoint, last_error)
        except Exception as e:
            last_error = str(e)
            log.debug("%s request failed: %s", endpoint, e)
    raise RuntimeError(f"Failed to get authKey: {last_error}")
