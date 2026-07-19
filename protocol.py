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
    return _ttlv_byte_stuff(b"\xaa\xaa" + struct.pack(">H", length) + bytes([crc]) + inner)


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
    payload = data[9 : 4 + pkt_len] if len(data) >= 4 + pkt_len else data[9:]
    return {"cmd": cmd, "packet_id": pid, "payload": payload}


def _ttlv_parse_fields(payload: bytes) -> list:
    """Parse TTLV fields from decrypted payload. Returns list of (id, type, value)."""
    fields = []
    i = 0
    while i < len(payload) - 1:
        tag_word = struct.unpack(">H", payload[i : i + 2])[0]
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
            val = int.from_bytes(payload[i : i + byte_count], "big")
            i += byte_count
            if sign:
                val = -val
            if decimals > 0:
                val = val / (10**decimals)
            fields.append((tag_id, "NUM", val))
        elif tag_type in (3, 5):  # Bytes
            if i + 2 > len(payload):
                break
            dlen = struct.unpack(">H", payload[i : i + 2])[0]
            i += 2
            fields.append((tag_id, "BYTES", payload[i : i + dlen]))
            i += dlen
        elif tag_type == 4:  # Struct/Array header
            if i + 2 > len(payload):
                break
            count = struct.unpack(">H", payload[i : i + 2])[0]
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
