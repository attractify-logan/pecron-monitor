#!/usr/bin/env python3
"""
Pecron Battery Monitor & Controller — real-time monitoring and control
via local TCP (LAN) with cloud MQTT fallback. Works with any Pecron power station.

Usage:
    python pecron_monitor.py --setup        # Interactive setup wizard
    python pecron_monitor.py                # Start monitoring
    python pecron_monitor.py --local        # Run in offline/local-only mode (no cloud)
    python pecron_monitor.py --status       # One-shot status check
    python pecron_monitor.py --ac on        # Turn AC output on
    python pecron_monitor.py --ac off       # Turn AC output off
    python pecron_monitor.py --dc on        # Turn DC output on
    python pecron_monitor.py --dc off       # Turn DC output off
    python pecron_monitor.py --controls     # List all available controls for your model
    python pecron_monitor.py --control ac_switch_hm on   # Set any control by code
    python pecron_monitor.py --raw          # Dump raw JSON from device
    python pecron_monitor.py --homeassistant # Start with Home Assistant MQTT bridge
"""

__version__ = "0.5.0"

import argparse
import base64
import hashlib
import json
import logging
import os
import secrets
import signal
import socket
import string
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# Local TCP transport (LAN-first, cloud-fallback)
try:
    from local_transport import LocalTransport, get_auth_key
    HAS_LOCAL = True
except ImportError:
    HAS_LOCAL = False

try:
    from local_transport import BLETransport, scan_ble_devices, HAS_BLE
except ImportError:
    HAS_BLE = False

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Missing dependency: pip install paho-mqtt")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Region configurations
# ---------------------------------------------------------------------------
REGIONS = {
    "na": {
        "name": "North America",
        "base_url": "https://iot-api.landecia.com",
        "mqtt_host": "iot-south.landecia.com",
        "mqtt_port": 8443,
        "mqtt_path": "/ws/v2",
        "user_domain": "U.DM.10351.1",
        "user_domain_secret": "HARsQXfeex8vxyaPRAM8fyjqqVuH2uxAGQ3inJ8XxTiB",
    },
    "eu": {
        "name": "Europe",
        "base_url": "https://iot-api.acceleronix.io",
        "mqtt_host": "iot-south.quecteleu.com",
        "mqtt_port": 8443,
        "mqtt_path": "/ws/v2",
        "user_domain": "C.DM.10351.1",
        "user_domain_secret": "FA5ZHXSka8y9GHvU91Hz1vWvaDSHE2mGW5B7bpn3fXTW",
    },
    "cn": {
        "name": "China",
        "base_url": "https://iot-api.quectelcn.com",
        "mqtt_host": "iot-south.quectelcn.com",
        "mqtt_port": 8443,
        "mqtt_path": "/ws/v2",
        "user_domain": "C.DM.5903.1",
        "user_domain_secret": "EufftRJSuWuVY7c6txzGifV9bJcfXHAFa7hXY5doXSn7",
    },
}

# ---------------------------------------------------------------------------
# Data point IDs (from Quectel TSL — Thing Specification Language)
# These are universal for each Pecron product model.
# The TSL is fetched dynamically; these are E1500LFP defaults as fallback.
# ---------------------------------------------------------------------------
DEFAULT_CONTROLS = {
    "ac_switch_hm":           {"id": 40, "type": "BOOL", "desc": "AC output"},
    "dc_switch_hm":           {"id": 38, "type": "BOOL", "desc": "DC output"},
    "ups_status_hm":          {"id": 27, "type": "BOOL", "desc": "UPS mode"},
    "auto_light_flag_as":     {"id": 43, "type": "BOOL", "desc": "Auto screen light"},
    "machine_screen_light_as":{"id": 45, "type": "ENUM", "desc": "Screen brightness"},
}

# Common sensor field mappings — works across all known Pecron models.
# Each sensor maps to a list of paths to try (first match wins).
# Some models (E1500LFP) nest battery/voltage in host_packet_data_jdb,
# while others (E300LFP) report them at the top level.
SENSOR_FIELDS = {
    "battery_percent": [
        ("host_packet_data_jdb", "host_packet_electric_percentage"),
        ("battery_percentage",),
    ],
    "voltage": [
        ("host_packet_data_jdb", "host_packet_voltage"),
    ],
    "temperature": [
        ("host_packet_data_jdb", "host_packet_temp"),
    ],
    "charge_status": [
        ("host_packet_data_jdb", "host_packet_status"),
    ],
    "total_input_power": [("total_input_power",)],
    "total_output_power": [("total_output_power",)],
    "remain_time": [("remain_time",)],
    "ac_output_power": [("ac_data_output_hm", "ac_output_power")],
    "ac_output_voltage": [("ac_data_output_hm", "ac_output_voltage")],
    "dc_output_power": [("dc_data_output_hm", "dc_output_power")],
    "ac_input_power": [("ac_data_input_hm", "ac_power")],
    "dc_input_power": [("dc_data_input_hm", "dc_input_power")],
    "ac_switch": [("ac_switch_hm",)],
    "dc_switch": [("dc_switch_hm",)],
    "ups_mode": [("ups_status_hm",)],
}


def _get_kv(kv: dict, paths, default=None):
    """Safely navigate nested kv dict. Accepts a single path tuple or a list of paths to try."""
    if not paths:
        return default
    # If it's a list of tuples, try each path
    if isinstance(paths, list):
        for path in paths:
            result = _get_kv_single(kv, path)
            if result is not None:
                return result
        return default
    # Single tuple path
    return _get_kv_single(kv, paths) if _get_kv_single(kv, paths) is not None else default


def _get_kv_single(kv: dict, path: tuple):
    """Safely navigate nested kv dict by a single path tuple."""
    obj = kv
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
        if obj is None:
            return None
    return obj

CONFIG_PATH = Path(__file__).parent / "config.yaml"
log = logging.getLogger("pecron")


# ===========================================================================
# Authentication
# ===========================================================================

def _make_auth_params(email: str, password: str, region: dict) -> dict:
    rand = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    md5 = hashlib.md5(rand.encode()).hexdigest().upper()
    aes_key = md5[8:24]
    iv = aes_key[8:16] + aes_key[0:8]
    cipher = AES.new(aes_key.encode(), AES.MODE_CBC, iv.encode())
    enc_pwd = base64.b64encode(cipher.encrypt(pad(password.encode(), 16))).decode()
    sig_input = email + enc_pwd + rand + region["user_domain_secret"]
    signature = hashlib.sha256(sig_input.encode()).hexdigest()
    return {
        "email": email, "pwd": enc_pwd, "random": rand,
        "userDomain": region["user_domain"], "signature": signature,
    }


def login(email: str, password: str, region: dict) -> dict:
    params = _make_auth_params(email, password, region)
    url = region["base_url"] + "/v2/enduser/enduserapi/emailPwdLogin"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if body.get("code") != 200:
        raise RuntimeError(f"Login failed: {body.get('msg', body)}")
    token_data = body["data"]["accessToken"]
    token = token_data["token"]
    jwt_parts = token.split(".")
    payload_b64 = jwt_parts[1] + "=" * (4 - len(jwt_parts[1]) % 4)
    jwt_payload = json.loads(base64.b64decode(payload_b64))
    return {
        "token": token, "uid": jwt_payload["uid"],
        "expires_at": jwt_payload.get("exp", 0),
    }


# ===========================================================================
# Device discovery
# ===========================================================================

def get_product_catalog(token: str, region: dict) -> dict:
    url = region["base_url"] + "/v2/enduser/enduserapi/getProductList?pageNum=1&pageSize=100"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if body.get("code") != 200:
        return {}
    return {p["productKey"]: p["name"] for p in body.get("data", {}).get("list", [])}


def get_user_devices(token: str, region: dict) -> list:
    """Get devices already bound to the user's account (correct pk/dk pairs).
    
    This is the most reliable way to discover devices — returns the exact
    product_key and device_key that the cloud has on file, avoiding
    mismatches that cause 'device is not bound' (4007) errors.
    """
    url = region["base_url"] + "/v2/binding/enduserapi/userDeviceList"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") != 200:
            log.debug("userDeviceList failed: %s", body.get("msg", body))
            return []
        data = body.get("data", {})
        device_list = data.get("list", data) if isinstance(data, dict) else data
        if not isinstance(device_list, list):
            return []
        devices = []
        for d in device_list:
            pk = d.get("productKey", "")
            dk = d.get("deviceKey", "")
            name = d.get("productName", d.get("deviceName", "Unknown"))
            if pk and dk:
                devices.append({"product_key": pk, "device_key": dk, "name": name})
        return devices
    except Exception as e:
        log.debug("userDeviceList request failed: %s", e)
        return []


def get_product_tsl(token: str, region: dict, product_key: str) -> dict:
    """Fetch TSL (data model) for a product — gives us data point IDs."""
    url = region["base_url"] + f"/v2/binding/enduserapi/productTSL?pk={product_key}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") == 200:
            tsl = body["data"]
            controls = {}
            for prop in tsl.get("properties", []):
                dt = prop.get("dataType", {})
                dtype = dt.get("type", dt) if isinstance(dt, dict) else str(dt)
                access = prop.get("subType", prop.get("accessMode", "R"))
                controls[prop["code"]] = {
                    "id": prop["id"], "type": dtype,
                    "desc": prop.get("name", prop["code"]),
                    "access": access,
                }
            return controls
    except Exception as e:
        log.debug("TSL fetch failed: %s", e)
    return {}


def verify_device(token: str, region: dict, product_key: str, device_key: str) -> dict:
    url = (region["base_url"] +
           f"/v2/binding/enduserapi/getDeviceBindingInfo?pk={product_key}&dk={device_key}")
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") == 200:
            return body.get("data", {})
    except Exception:
        pass
    return {}


def get_device_online_status(token: str, region: dict, product_key: str, device_key: str) -> dict:
    """Check if device is online via cloud API."""
    url = (region["base_url"] +
           f"/v2/binding/enduserapi/getDeviceOnlineStatus?pk={product_key}&dk={device_key}")
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") == 200:
            return body.get("data", {})
    except Exception as e:
        log.debug("Online status check failed: %s", e)
    return {}


def get_device_properties_rest(token: str, region: dict, pk: str, dk: str) -> dict:
    """Read device properties via REST API (same method as ha-pecron HACS addon).

    This is a reliable fallback when MQTT doesn't deliver data — it queries the
    cloud API directly for current device state.

    Returns a kv dict compatible with _process_data().
    """
    url = (region["base_url"] +
           f"/v2/binding/enduserapi/getDeviceBusinessAttributes?pk={pk}&dk={dk}")
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") != 200:
            log.debug("REST properties failed: %s", body.get("msg", body))
            return {}

        tsl_info = body.get("data", {}).get("customizeTslInfo", [])
        if not tsl_info:
            log.debug("REST properties returned empty TSL info")
            return {}

        # Convert TSL info array to kv dict matching MQTT format
        kv = {}
        for item in tsl_info:
            code = item.get("code", "")
            value = item.get("value")

            if value is None:
                continue

            # Handle struct types (nested dicts)
            if isinstance(value, dict):
                kv[code] = value
            elif isinstance(value, list):
                kv[code] = value
            elif isinstance(value, bool):
                kv[code] = value
            elif isinstance(value, (int, float)):
                kv[code] = value
            else:
                try:
                    kv[code] = int(value)
                except (ValueError, TypeError):
                    kv[code] = value

        return kv
    except Exception as e:
        log.debug("REST properties request failed: %s", e)
        return {}


def resolve_devices(config: dict, token: str, region: dict) -> list:
    catalog = get_product_catalog(token, region)
    devices = []
    configured = config.get("devices", [])
    if not configured:
        raise RuntimeError(
            "No devices configured. Run --setup or add devices to config.yaml.\n"
            "Find your device key in the Pecron app: Device → Settings → Device Info"
        )
    for d in configured:
        pk = d.get("product_key", "")
        dk = d.get("device_key", "")
        name = catalog.get(pk, d.get("name", "Unknown"))
        info = verify_device(token, region, pk, dk)
        if info:
            # Fetch TSL for this product
            tsl = get_product_tsl(token, region, pk)
            api_name = info.get("productName", name)
            devices.append({
                "product_key": pk, "device_key": dk,
                "device_name": api_name,
                "product_name": api_name,
                "controls": tsl or DEFAULT_CONTROLS,
            })
            # Check online status
            online_info = get_device_online_status(token, region, pk, dk)
            online = online_info.get("online", online_info.get("value"))
            if online:
                log.info("  ✅ %s (pk=%s, dk=%s) — ONLINE", api_name, pk, dk)
            else:
                log.warning("  ⚠️  %s (pk=%s, dk=%s) — OFFLINE (device may not be connected to WiFi/internet)", api_name, pk, dk)
                log.warning("     MQTT monitoring will not receive data until the device is online.")
                log.warning("     Check: Is the device powered on? Is it connected to WiFi?")
                log.warning("     In the Pecron app, go to the device — if it shows 'offline', the device can't reach the cloud.")
            if api_name != name and name != "Unknown":
                log.info("     ℹ️  API identifies this as '%s' (config says '%s')", api_name, name)
        else:
            # Try to find correct pk from userDeviceList
            account_devs = get_user_devices(token, region)
            corrected = None
            for ad in account_devs:
                if ad["device_key"].upper() == dk.upper():
                    corrected = ad
                    break
            if corrected and corrected["product_key"] != pk:
                log.warning("  ⚠️  %s (%s) — wrong product_key in config (pk=%s)", name, dk, pk)
                log.info("     Auto-correcting to pk=%s (from account device list)", corrected["product_key"])
                pk = corrected["product_key"]
                tsl = get_product_tsl(token, region, pk)
                devices.append({
                    "product_key": pk, "device_key": dk,
                    "device_name": corrected["name"],
                    "product_name": corrected["name"],
                    "controls": tsl or DEFAULT_CONTROLS,
                })
                log.info("  ✅ %s (pk=%s, dk=%s)", corrected["name"], pk, dk)
            else:
                log.warning("  ❌ %s (%s) — not found or not bound", name, dk)
                log.warning("     Check that your device_key is correct (Pecron app → Device → ⚙️ → Device Info → Device Key/Code)")
                log.warning("     It should be 12 hex characters (your device's MAC address)")
    return devices


# ===========================================================================
# TTLV protocol
# ===========================================================================

def _encode_varint(val: int) -> bytes:
    if val == 0:
        return b'\x00'
    result = []
    while val > 0:
        result.append(val & 0xFF)
        val >>= 8
    return bytes(reversed(result))


def _build_packet(packet_id: int, cmd: int, payload: bytes = b'') -> bytes:
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
# Home Assistant MQTT Discovery
# ===========================================================================

class HomeAssistantBridge:
    """Publishes Home Assistant MQTT auto-discovery config and state updates."""

    def __init__(self, ha_config: dict, devices: list):
        self.ha_config = ha_config
        self.devices = devices
        self.client = None
        self.discovery_prefix = ha_config.get("discovery_prefix", "homeassistant")
        self._connected = False

    def connect(self):
        host = self.ha_config.get("mqtt_host", "localhost")
        port = self.ha_config.get("mqtt_port", 1883)
        user = self.ha_config.get("mqtt_user", "")
        pw = self.ha_config.get("mqtt_password", "")

        self.client = mqtt.Client(
            client_id="pecron_ha_bridge",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if user:
            self.client.username_pw_set(user, pw)

        def on_connect(client, ud, flags, rc, props=None):
            if rc == mqtt.CONNACK_ACCEPTED:
                self._connected = True
                log.info("Home Assistant MQTT bridge connected to %s:%d", host, port)
                self._publish_discovery()
                # Subscribe to command topics
                for device in self.devices:
                    dk = device["device_key"]
                    for ctrl in ["ac", "dc", "ups"]:
                        client.subscribe(f"pecron/{dk}/{ctrl}/set", qos=1)

        def on_message(client, ud, msg):
            # Handle HA commands
            parts = msg.topic.split("/")
            if len(parts) == 4 and parts[3] == "set":
                dk = parts[1]
                ctrl = parts[2]
                payload = msg.payload.decode().upper()
                self._handle_command(dk, ctrl, payload)

        self.client.on_connect = on_connect
        self.client.on_message = on_message
        self.client.connect(host, port)
        self.client.loop_start()

    def _handle_command(self, device_key: str, control: str, payload: str):
        """Called when HA sends a command. Delegates to the monitor."""
        # This will be wired up by PecronMonitor
        if hasattr(self, 'command_callback'):
            self.command_callback(device_key, control, payload == "ON")

    def _publish_discovery(self):
        """Publish HA MQTT auto-discovery messages."""
        for device in self.devices:
            dk = device["device_key"]
            name = device["device_name"]
            dev_info = {
                "identifiers": [f"pecron_{dk}"],
                "name": f"Pecron {name}",
                "manufacturer": "Pecron",
                "model": name,
            }

            # Battery sensor
            self._pub_config("sensor", dk, "battery", {
                "name": "Battery",
                "device_class": "battery",
                "unit_of_measurement": "%",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.battery_percent }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_battery",
            })

            # Voltage sensor
            self._pub_config("sensor", dk, "voltage", {
                "name": "Voltage",
                "device_class": "voltage",
                "unit_of_measurement": "V",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.voltage }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_voltage",
            })

            # Temperature sensor
            self._pub_config("sensor", dk, "temperature", {
                "name": "Temperature",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.temperature }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_temperature",
            })

            # Power in/out sensors
            for key, label in [("total_input", "Input Power"), ("total_output", "Output Power")]:
                self._pub_config("sensor", dk, key, {
                    "name": label,
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.{key}_power }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_{key}",
                })

            # Remaining time sensor
            self._pub_config("sensor", dk, "remaining", {
                "name": "Remaining Time",
                "icon": "mdi:timer-outline",
                "unit_of_measurement": "min",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.remain_minutes }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_remaining",
            })

            # AC switch
            self._pub_config("switch", dk, "ac", {
                "name": "AC Output",
                "icon": "mdi:power-plug",
                "state_topic": f"pecron/{dk}/state",
                "command_topic": f"pecron/{dk}/ac/set",
                "value_template": "{{ value_json.ac_switch }}",
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_ac",
            })

            # DC switch
            self._pub_config("switch", dk, "dc", {
                "name": "DC Output",
                "icon": "mdi:usb-port",
                "state_topic": f"pecron/{dk}/state",
                "command_topic": f"pecron/{dk}/dc/set",
                "value_template": "{{ value_json.dc_switch }}",
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_dc",
            })

            # UPS switch
            self._pub_config("switch", dk, "ups", {
                "name": "UPS Mode",
                "icon": "mdi:shield-battery",
                "state_topic": f"pecron/{dk}/state",
                "command_topic": f"pecron/{dk}/ups/set",
                "value_template": "{{ value_json.ups_mode }}",
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_ups",
            })

        log.info("Published Home Assistant discovery configs")

    def _pub_config(self, component: str, dk: str, key: str, config: dict):
        topic = f"{self.discovery_prefix}/{component}/pecron_{dk}/{key}/config"
        self.client.publish(topic, json.dumps(config), qos=1, retain=True)

    def publish_state(self, device_key: str, kv: dict):
        """Publish current state to HA."""
        if not self._connected:
            return

        state = {
            "battery_percent": int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], 0)),
            "voltage": round(float(_get_kv(kv, SENSOR_FIELDS["voltage"], 0)), 1),
            "temperature": int(_get_kv(kv, SENSOR_FIELDS["temperature"], 0)),
            "total_input_power": int(_get_kv(kv, SENSOR_FIELDS["total_input_power"], 0)),
            "total_output_power": int(_get_kv(kv, SENSOR_FIELDS["total_output_power"], 0)),
            "remain_minutes": int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0)),
            "ac_switch": "ON" if _get_kv(kv, SENSOR_FIELDS["ac_switch"]) else "OFF",
            "dc_switch": "ON" if _get_kv(kv, SENSOR_FIELDS["dc_switch"]) else "OFF",
            "ups_mode": "ON" if _get_kv(kv, SENSOR_FIELDS["ups_mode"]) else "OFF",
            "ac_output_power": int(_get_kv(kv, SENSOR_FIELDS["ac_output_power"], 0)),
            "ac_output_voltage": int(_get_kv(kv, SENSOR_FIELDS["ac_output_voltage"], 0)),
        }

        self.client.publish(f"pecron/{device_key}/state", json.dumps(state), qos=1, retain=True)

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


# ===========================================================================
# Main monitor
# ===========================================================================

class PecronMonitor:
    def __init__(self, config: dict):
        self.config = config
        self.region = REGIONS[config["region"]]
        self.token_data = None
        self.mqtt_client = None
        self.devices = []
        self.latest_data = {}
        self.data_sources = {}  # device_key → "BLE" | "LOCAL TCP" | "CLOUD MQTT" | "REST API"
        self.last_alert = {}
        self._packet_id = 0
        self._running = False
        self.ha_bridge = None
        self.local_transports = {}  # device_key → LocalTransport
        self.ble_transports = {}   # device_key → BLETransport
        self.offline_mode = False  # Set to True when running in local-only mode

        # Automation rules
        self.rules = config.get("rules", [])

    def _next_packet_id(self) -> int:
        self._packet_id = (self._packet_id + 1) % 65535
        return self._packet_id

    def authenticate(self, force_offline: bool = False):
        """Authenticate and set up transports.

        Args:
            force_offline: If True, skip cloud login and use cached config only.
                          Auto-detected when all devices have local credentials.
        """
        # Check if we can run fully offline
        can_offline = self._check_offline_capable()

        if force_offline:
            if not can_offline:
                raise RuntimeError(
                    "Cannot run in offline mode: missing required fields.\n"
                    "Each device needs: lan_ip or ble_address, auth_key, product_key, device_key.\n"
                    "Run --setup first to fetch and cache these credentials."
                )
            self.offline_mode = True
            log.info("🔒 OFFLINE MODE — using cached credentials from config.yaml")
            self._build_devices_from_config()
        elif not force_offline and can_offline:
            # Try cloud first, graceful offline fallback
            try:
                log.info("Logging in to Pecron cloud (%s)...", self.region["name"])
                self.token_data = login(self.config["email"], self.config["password"], self.region)
                log.info("Logged in as %s", self.token_data["uid"])
                log.info("Resolving devices...")
                self.devices = resolve_devices(self.config, self.token_data["token"], self.region)
                if not self.devices:
                    raise RuntimeError("No valid devices found.")
            except Exception as e:
                log.warning("Cloud login failed (%s), falling back to offline mode", e)
                self.offline_mode = True
                self._build_devices_from_config()
        else:
            # Normal cloud-first mode
            log.info("Logging in to Pecron cloud (%s)...", self.region["name"])
            self.token_data = login(self.config["email"], self.config["password"], self.region)
            log.info("Logged in as %s", self.token_data["uid"])
            log.info("Resolving devices...")
            self.devices = resolve_devices(self.config, self.token_data["token"], self.region)
            if not self.devices:
                raise RuntimeError("No valid devices found.")

    def _check_offline_capable(self) -> bool:
        """Check if all devices have the required fields for offline operation."""
        configured = self.config.get("devices", [])
        if not configured:
            return False
        for d in configured:
            has_transport = d.get("lan_ip") or d.get("ble_address") or d.get("ble")
            has_auth = d.get("auth_key")
            has_ids = d.get("product_key") and d.get("device_key")
            if not (has_transport and has_auth and has_ids):
                return False
        return True

    def _build_devices_from_config(self):
        """Build device list from config.yaml when running offline."""
        configured = self.config.get("devices", [])
        if not configured:
            raise RuntimeError("No devices in config.yaml")

        for d in configured:
            pk = d["product_key"]
            dk = d["device_key"]
            name = d.get("name", "Unknown")

            # Load cached TSL if available, otherwise use defaults
            controls = d.get("tsl_cache", DEFAULT_CONTROLS)

            self.devices.append({
                "product_key": pk,
                "device_key": dk,
                "device_name": name,
                "product_name": name,
                "controls": controls,
            })
            log.info("  📦 Loaded from config: %s (pk=%s, dk=%s)", name, pk, dk)

        log.info("Loaded %d device(s) from config", len(self.devices))

        # Set up local transports for devices with LAN IPs
        if HAS_LOCAL:
            configured = {d.get("device_key"): d for d in self.config.get("devices", [])}
            for device in self.devices:
                dk = device["device_key"]
                cfg = configured.get(dk, {})
                lan_ip = cfg.get("lan_ip")
                if not lan_ip:
                    continue
                try:
                    auth_key = cfg.get("auth_key")
                    if not auth_key:
                        log.info("Fetching auth key for %s...", dk)
                        auth_key = get_auth_key(
                            self.token_data["token"], self.region,
                            device["product_key"], dk
                        )
                        log.info("Got auth key for %s (cache it in config.yaml as auth_key)", dk)
                    self.local_transports[dk] = LocalTransport(lan_ip, auth_key)
                    log.info("Local transport configured for %s @ %s", dk, lan_ip)
                except Exception as e:
                    log.warning("Failed to set up local transport for %s: %s", dk, e)

        # Set up BLE transports
        if HAS_BLE:
            for device in self.devices:
                dk = device["device_key"]
                cfg = configured.get(dk, {})
                ble_addr = cfg.get("ble_address")
                ble_enabled = cfg.get("ble", False)
                if not ble_addr and not ble_enabled:
                    continue
                try:
                    auth_key = cfg.get("auth_key")
                    if not auth_key and dk not in self.local_transports:
                        log.info("Fetching auth key for %s (BLE)...", dk)
                        auth_key = get_auth_key(
                            self.token_data["token"], self.region,
                            device["product_key"], dk
                        )
                    elif not auth_key:
                        # Reuse from TCP transport config
                        auth_key = cfg.get("auth_key", self.local_transports[dk].auth_key_b64
                                           if dk in self.local_transports else None)
                    if auth_key:
                        self.ble_transports[dk] = BLETransport(
                            auth_key, device_address=ble_addr, device_key=dk
                        )
                        log.info("BLE transport configured for %s%s", dk,
                                 f" @ {ble_addr}" if ble_addr else " (will scan)")
                except Exception as e:
                    log.warning("Failed to set up BLE transport for %s: %s", dk, e)

    def _connect_local(self, device_key: str) -> bool:
        """Try to connect local transport for a device."""
        lt = self.local_transports.get(device_key)
        if lt and not lt.connected:
            try:
                return lt.connect()
            except Exception as e:
                log.debug("Local connect failed for %s: %s", device_key, e)
        return lt.connected if lt else False

    def _channel_id(self, device: dict) -> str:
        return f"qd{device['product_key']}{device['device_key']}"

    def _find_device(self, device_key: str) -> dict:
        for d in self.devices:
            if d["device_key"] == device_key:
                return d
        return {}

    # --- MQTT callbacks ---

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != mqtt.CONNACK_ACCEPTED:
            log.error("MQTT connection failed: %s", mqtt.connack_string(rc))
            return
        log.info("MQTT connected")
        for device in self.devices:
            cid = self._channel_id(device)
            for suffix in ["bus_", "ack_", "onl_"]:
                topic = f"q/2/d/{cid}/{suffix}"
                client.subscribe(topic, qos=1)
                log.debug("  Subscribed: %s", topic)
            log.info("Subscribed to %s (pk=%s, dk=%s, channel=%s)",
                     device["device_name"], device["product_key"],
                     device["device_key"], cid)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("Non-JSON MQTT message on %s (%d bytes)", msg.topic, len(msg.payload))
            return

        topic_suffix = msg.topic.split("/")[-1]
        device_key = payload.get("deviceKey", "")
        log.debug("MQTT message: topic=%s suffix=%s dk=%s keys=%s",
                  msg.topic, topic_suffix, device_key, list(payload.keys()))

        if topic_suffix == "bus_" and "data" in payload:
            kv = payload["data"].get("kv", {})
            if kv:
                self.latest_data[device_key] = kv
                self._process_data(device_key, kv, source="CLOUD MQTT")
            else:
                log.debug("bus_ message with empty kv: %s", list(payload["data"].keys()))
        elif topic_suffix == "onl_" and "data" in payload:
            online = payload["data"].get("value", 0) == 1
            log.info("Device %s is now %s", device_key, "online" if online else "offline")
        elif topic_suffix == "ack_":
            log.debug("ACK received for device %s", device_key)
        elif topic_suffix == "sys_":
            # System messages (responses to our publishes, device online/offline events)
            code = payload.get("code")
            msg_text = payload.get("msg", "")
            msg_type = payload.get("type", "")
            if code == 4007:
                if not hasattr(self, '_4007_warned'):
                    self._4007_warned = True
                    log.error("Cloud says 'device is not bound' (code 4007).")
                    log.error("This usually means the wrong product_key is configured.")
                    log.error("Your device model may have multiple product keys in Pecron's system.")
                    log.error("Fix: Run 'python pecron_monitor.py --setup' and use auto-detect (option 1).")
                    log.error("If auto-detect finds multiple matches, try each one until MQTT data flows.")
            elif code and code != 200:
                log.warning("Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type)
            else:
                log.debug("Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type)

    # --- Data processing ---

    def _process_data(self, device_key: str, kv: dict, source: str = "UNKNOWN"):
        """Process device data and log the source.

        Args:
            device_key: Device key
            kv: Data dict
            source: One of "BLE", "LOCAL TCP", "CLOUD MQTT", "REST API"
        """
        battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
        voltage = float(_get_kv(kv, SENSOR_FIELDS["voltage"], 0))
        temp = int(_get_kv(kv, SENSOR_FIELDS["temperature"], 0))
        total_in = int(_get_kv(kv, SENSOR_FIELDS["total_input_power"], 0))
        total_out = int(_get_kv(kv, SENSOR_FIELDS["total_output_power"], 0))
        remain = int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0))

        # Skip processing if data is clearly invalid (no real reading)
        if battery_pct < 0 and voltage == 0 and total_in == 0 and total_out == 0:
            log.debug("Skipping invalid/empty data for %s (battery=%d%%, voltage=%.1fV)",
                      device_key, battery_pct, voltage)
            return

        # Track data source
        self.data_sources[device_key] = source

        log.info("🔋 %s%% | %.1fV | %d°C | ⚡ In:%dW Out:%dW | ⏱ %dh%dm [via %s]",
                 battery_pct, voltage, temp, total_in, total_out,
                 remain // 60, remain % 60, source)

        # Publish to Home Assistant
        if self.ha_bridge:
            self.ha_bridge.publish_state(device_key, kv)

        # Check alert thresholds
        self._check_alerts(device_key, battery_pct, voltage, remain)

        # Evaluate automation rules
        self._evaluate_rules(device_key, kv, battery_pct)

    def _check_alerts(self, device_key, battery_pct, voltage, remain):
        alerts = self.config.get("alerts", {})
        threshold = alerts.get("low_battery_percent", 20)
        cooldown = alerts.get("cooldown_minutes", 30) * 60
        if battery_pct >= 0 and battery_pct <= threshold:
            now = time.time()
            last = self.last_alert.get(device_key, 0)
            if now - last > cooldown:
                self.last_alert[device_key] = now
                self._send_alert(device_key, battery_pct, voltage, remain)

    def _send_alert(self, device_key, battery_pct, voltage, remain_min):
        msg = (f"⚠️ Pecron Low Battery Alert\n"
               f"Battery: {battery_pct}%\nVoltage: {voltage:.1f}V\n"
               f"Remaining: {remain_min // 60}h {remain_min % 60}m\n"
               f"Device: {device_key}")
        log.warning(msg)
        alerts = self.config.get("alerts", {})

        tg = alerts.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            try:
                url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
                data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": msg}).encode()
                urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
            except Exception as e:
                log.error("Telegram alert failed: %s", e)

        ntfy = alerts.get("ntfy", {})
        if ntfy.get("enabled") and ntfy.get("url"):
            try:
                req = urllib.request.Request(ntfy["url"], data=msg.encode(),
                                             headers={"Title": f"Pecron Battery {battery_pct}%"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.error("ntfy alert failed: %s", e)

        wh = alerts.get("webhook", {})
        if wh.get("enabled") and wh.get("url"):
            try:
                payload = json.dumps({"battery_percent": battery_pct, "voltage": voltage,
                                       "remain_minutes": remain_min, "device_key": device_key,
                                       "message": msg}).encode()
                req = urllib.request.Request(wh["url"], data=payload,
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.error("Webhook alert failed: %s", e)

    # --- Control commands ---

    def send_control(self, device_key: str, control_code: str, value):
        """Send a control command. Auto-detects type from TSL (BOOL, ENUM, INT)."""
        device = self._find_device(device_key)
        if not device:
            log.error("Device %s not found", device_key)
            return False

        controls = device.get("controls", DEFAULT_CONTROLS)
        ctrl = controls.get(control_code)
        if not ctrl:
            log.error("Control %s not found for device %s", control_code, device_key)
            return False

        access = ctrl.get("access", "R").upper()
        if "W" not in access:
            log.error("Control %s is read-only (access=%s)", control_code, access)
            return False

        cid = self._channel_id(device)
        pid = self._next_packet_id()
        ctrl_type = str(ctrl.get("type", "BOOL")).upper()

        if ctrl_type == "BOOL":
            pkt = build_ttlv_write_bool(pid, ctrl["id"], bool(value))
        elif ctrl_type in ("ENUM", "INT", "LONG"):
            pkt = build_ttlv_write_enum(pid, ctrl["id"], int(value))
        else:
            log.warning("Unknown control type '%s' for %s, trying bool", ctrl_type, control_code)
            pkt = build_ttlv_write_bool(pid, ctrl["id"], bool(value))

        # Try BLE first
        ble = self.ble_transports.get(device_key)
        if ble and ble.connected:
            try:
                if ble.send_control(ctrl["id"], value, ctrl_type):
                    log.info("Sent %s=%s (type=%s) to %s via BLE", control_code, value, ctrl_type, device_key)
                    return True
            except Exception as e:
                log.warning("BLE control failed: %s", e)

        # Try TCP/WiFi local transport
        lt = self.local_transports.get(device_key)
        if lt and lt.connected:
            try:
                if lt.send_control(ctrl["id"], value, ctrl_type):
                    log.info("Sent %s=%s (type=%s) to %s via TCP", control_code, value, ctrl_type, device_key)
                    return True
            except Exception as e:
                log.warning("TCP control failed: %s", e)

        # Fall back to cloud MQTT
        self.mqtt_client.publish(f"q/1/d/{cid}/bus", pkt, qos=1)
        log.info("Sent %s=%s (type=%s) to %s via CLOUD", control_code, value, ctrl_type, device_key)
        return True

    # Convenience aliases
    def send_bool_control(self, device_key: str, control_code: str, value: bool):
        return self.send_control(device_key, control_code, value)

    def set_ac(self, device_key: str, on: bool):
        return self.send_bool_control(device_key, "ac_switch_hm", on)

    def set_dc(self, device_key: str, on: bool):
        return self.send_bool_control(device_key, "dc_switch_hm", on)

    def set_ups(self, device_key: str, on: bool):
        return self.send_bool_control(device_key, "ups_status_hm", on)

    # --- Automation rules ---

    def _evaluate_rules(self, device_key: str, kv: dict, battery_pct: int):
        """Evaluate automation rules against current state."""
        for rule in self.rules:
            if rule.get("device_key") and rule["device_key"] != device_key:
                continue

            try:
                condition = rule.get("condition", {})
                action = rule.get("action", {})

                # Check condition
                triggered = False
                if "battery_below" in condition:
                    # Ignore invalid battery readings (-1 means no data)
                    if battery_pct < 0:
                        continue
                    triggered = battery_pct <= condition["battery_below"]
                elif "battery_above" in condition:
                    if battery_pct < 0:
                        continue
                    triggered = battery_pct >= condition["battery_above"]
                elif "input_power_below" in condition:
                    triggered = int(kv.get("total_input_power", 0)) <= condition["input_power_below"]
                elif "input_power_above" in condition:
                    triggered = int(kv.get("total_input_power", 0)) >= condition["input_power_above"]
                elif "schedule" in condition:
                    # Time-based: "HH:MM" format
                    now = datetime.now().strftime("%H:%M")
                    triggered = now == condition["schedule"]

                if not triggered:
                    continue

                # Check cooldown
                rule_id = rule.get("name", str(rule))
                cooldown = rule.get("cooldown_minutes", 5) * 60
                now_ts = time.time()
                last = self.last_alert.get(f"rule_{rule_id}", 0)
                if now_ts - last < cooldown:
                    continue
                self.last_alert[f"rule_{rule_id}"] = now_ts

                # Execute action
                target_dk = action.get("device_key", device_key)
                if "set_ac" in action:
                    self.set_ac(target_dk, action["set_ac"])
                    log.info("Rule '%s': set AC=%s on %s", rule.get("name"), action["set_ac"], target_dk)
                if "set_dc" in action:
                    self.set_dc(target_dk, action["set_dc"])
                    log.info("Rule '%s': set DC=%s on %s", rule.get("name"), action["set_dc"], target_dk)
                if "set_ups" in action:
                    self.set_ups(target_dk, action["set_ups"])
                    log.info("Rule '%s': set UPS=%s on %s", rule.get("name"), action["set_ups"], target_dk)

            except Exception as e:
                log.error("Rule evaluation error: %s", e)

    # --- Status request ---

    def _request_status(self):
        for device in self.devices:
            dk = device["device_key"]

            # Priority: BLE → TCP/WiFi → Cloud MQTT → REST API

            # Try BLE first (no infrastructure needed)
            ble = self.ble_transports.get(dk)
            if ble:
                if not ble.connected:
                    try:
                        ble.connect()
                    except Exception as e:
                        log.debug("BLE connect failed for %s: %s", dk, e)
                if ble.connected:
                    try:
                        kv = ble.read_status()
                        if kv:
                            log.debug("Got status via BLE for %s", dk)
                            self.latest_data[dk] = kv
                            self._process_data(dk, kv, source="BLE")
                            continue
                    except Exception as e:
                        log.warning("BLE read failed for %s: %s", dk, e)

            # Try TCP/WiFi local transport
            lt = self.local_transports.get(dk)
            if lt:
                if not lt.connected:
                    self._connect_local(dk)
                if lt.connected:
                    try:
                        kv = lt.read_status()
                        if kv:
                            log.debug("Got status via LOCAL TCP for %s", dk)
                            self.latest_data[dk] = kv
                            self._process_data(dk, kv, source="LOCAL TCP")
                            continue
                    except Exception as e:
                        log.warning("Local TCP read failed for %s: %s", dk, e)

            # Try cloud MQTT (request data - actual response arrives via _on_message)
            if self.mqtt_client:
                cid = self._channel_id(device)
                pkt = build_ttlv_read(self._next_packet_id())
                topic = f"q/1/d/{cid}/bus"
                result = self.mqtt_client.publish(topic, pkt, qos=1)
                log.debug("Published TTLV read to %s (rc=%s, mid=%s)",
                          topic, result.rc, result.mid)

            # If we haven't received MQTT data for this device yet, try REST API
            if dk not in self.latest_data:
                if self.token_data:  # Only available if not in offline mode
                    log.debug("No MQTT data for %s yet, trying REST API fallback...", dk)
                    kv = get_device_properties_rest(
                        self.token_data["token"], self.region,
                        device["product_key"], dk
                    )
                    if kv:
                        log.info("Got status via REST API for %s", dk)
                        self.latest_data[dk] = kv
                        self._process_data(dk, kv, source="REST API")

    def _token_needs_refresh(self) -> bool:
        if not self.token_data:
            return True
        return time.time() > (self.token_data["expires_at"] - 300)

    # --- MQTT connection ---

    def connect_mqtt(self):
        if self.offline_mode:
            log.info("Offline mode — skipping MQTT connection")
            return

        client_id = f"qu_{self.token_data['uid']}_{int(time.time() * 1000)}"
        self.mqtt_client = mqtt.Client(
            client_id=client_id, transport="websockets",
            protocol=mqtt.MQTTv311, callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self.mqtt_client.ws_set_options(path=self.region["mqtt_path"])
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set(username="", password=self.token_data["token"])
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_message = self._on_message
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
        log.info("Connecting to MQTT broker %s:%d...",
                 self.region["mqtt_host"], self.region["mqtt_port"])
        self.mqtt_client.connect(self.region["mqtt_host"], self.region["mqtt_port"])
        self.mqtt_client.loop_start()

    # --- Main loop ---

    def run(self, enable_ha=False, force_offline=False):
        self._running = True
        self.authenticate(force_offline=force_offline)
        self.connect_mqtt()

        if enable_ha:
            ha_config = self.config.get("homeassistant", {})
            if ha_config.get("enabled") or enable_ha:
                self.ha_bridge = HomeAssistantBridge(ha_config, self.devices)
                self.ha_bridge.command_callback = self._ha_command
                self.ha_bridge.connect()

        poll_interval = self.config.get("poll_interval", 60)
        log.info("Monitoring started (polling every %ds)", poll_interval)

        time.sleep(3)
        self._request_status()

        try:
            while self._running:
                time.sleep(poll_interval)
                if self._token_needs_refresh():
                    log.info("Refreshing token...")
                    try:
                        self.mqtt_client.loop_stop()
                        self.mqtt_client.disconnect()
                    except Exception:
                        pass
                    self.authenticate()
                    self.connect_mqtt()
                    time.sleep(3)
                self._request_status()
        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            self._running = False
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            if self.ha_bridge:
                self.ha_bridge.disconnect()

    def _ha_command(self, device_key: str, control: str, on: bool):
        """Handle commands from Home Assistant."""
        ctrl_map = {"ac": "ac_switch_hm", "dc": "dc_switch_hm", "ups": "ups_status_hm"}
        code = ctrl_map.get(control)
        if code:
            self.send_bool_control(device_key, code, on)

    def one_shot_command(self, ac=None, dc=None, force_offline=False):
        """Connect, send a command, verify, and exit."""
        self.authenticate(force_offline=force_offline)
        if not self.offline_mode:
            self.connect_mqtt()
            time.sleep(3)
        else:
            time.sleep(1)  # Give local transports time to connect

        for device in self.devices:
            dk = device["device_key"]
            if ac is not None:
                self.set_ac(dk, ac)
            if dc is not None:
                self.set_dc(dk, dc)

        time.sleep(3)
        # Read back state to confirm
        self._request_status()
        time.sleep(5)

        for dk, kv in self.latest_data.items():
            ac_state = "ON" if kv.get("ac_switch_hm") else "OFF"
            dc_state = "ON" if kv.get("dc_switch_hm") else "OFF"
            print(f"Device {dk}: AC={ac_state} DC={dc_state}")

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

    def status_once(self, force_offline: bool = False):
        self.authenticate(force_offline=force_offline)
        if not self.offline_mode:
            self.connect_mqtt()
            time.sleep(3)
        self._request_status()
        time.sleep(5)

        for dk, kv in self.latest_data.items():
            remain = int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0))
            packs = kv.get("charging_pack_data_jdb", [])
            source = self.data_sources.get(dk, "UNKNOWN")

            print(f"\n{'=' * 50}")
            print(f"Device: {dk}")
            print(f"Connection: {source}")
            print(f"{'=' * 50}")
            print(f"Battery:       {_get_kv(kv, SENSOR_FIELDS['battery_percent'], '?')}%")
            print(f"Voltage:       {float(_get_kv(kv, SENSOR_FIELDS['voltage'], 0)):.1f}V")
            print(f"Temperature:   {_get_kv(kv, SENSOR_FIELDS['temperature'], '?')}°C")
            print(f"Remaining:     {remain // 60}h {remain % 60}m")
            print(f"Total Input:   {_get_kv(kv, SENSOR_FIELDS['total_input_power'], 0)}W")
            print(f"Total Output:  {_get_kv(kv, SENSOR_FIELDS['total_output_power'], 0)}W")
            print(f"AC Output:     {_get_kv(kv, SENSOR_FIELDS['ac_output_power'], 0)}W @ {_get_kv(kv, SENSOR_FIELDS['ac_output_voltage'], '?')}V")
            print(f"DC Output:     {_get_kv(kv, SENSOR_FIELDS['dc_output_power'], 0)}W")
            print(f"AC Input:      {_get_kv(kv, SENSOR_FIELDS['ac_input_power'], 0)}W")
            print(f"DC Input:      {_get_kv(kv, SENSOR_FIELDS['dc_input_power'], 0)}W")
            print(f"AC Switch:     {'ON' if _get_kv(kv, SENSOR_FIELDS['ac_switch']) else 'OFF'}")
            print(f"DC Switch:     {'ON' if _get_kv(kv, SENSOR_FIELDS['dc_switch']) else 'OFF'}")
            print(f"UPS Mode:      {'ON' if _get_kv(kv, SENSOR_FIELDS['ups_mode']) else 'OFF'}")

            for i, pack in enumerate(packs):
                if int(pack.get("charging_pack_status", 4)) != 4:
                    print(f"Pack {i}:        {pack.get('charging_pack_battery', '?')}% "
                          f"{float(pack.get('charging_pack_voltage', 0)):.1f}V")

        if not self.latest_data:
            print("No data received — device may be offline.")

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

    def stop(self):
        self._running = False


# ===========================================================================
# Setup wizard
# ===========================================================================

def _scan_lan_for_pecron(subnet: str = None, timeout: float = 0.3) -> list:
    """Scan local network for devices with TCP port 6607 open."""
    import ipaddress
    results = []
    if not subnet:
        # Try to detect subnet from default interface
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            # Assume /24
            net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
            subnet = str(net)
        except Exception:
            subnet = "192.168.1.0/24"

    print(f"  Scanning {subnet} for Pecron devices (port 6607)...")
    net = ipaddress.IPv4Network(subnet, strict=False)
    for host in net.hosts():
        ip = str(host)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            if sock.connect_ex((ip, 6607)) == 0:
                results.append(ip)
                print(f"  Found: {ip}")
            sock.close()
        except Exception:
            pass
    return results


def _setup_lan_discovery(devices: list, token: str, region: dict) -> list:
    """Interactive LAN setup: scan network, match devices, fetch auth keys.

    Returns the modified devices list with lan_ip and auth_key added.
    """
    found_ips = _scan_lan_for_pecron()

    if not found_ips:
        print("  No Pecron devices found on LAN.")
        manual_ip = input("  Enter device IP manually (or press Enter to skip): ").strip()
        if manual_ip:
            found_ips = [manual_ip]
        else:
            return devices

    for device in devices:
        dk = device["device_key"]
        if len(found_ips) == 1:
            ip = found_ips[0]
            print(f"  Assigning {ip} to {device.get('name', dk)}")
        else:
            print(f"\n  Multiple Pecron devices found. Which IP is {device.get('name', dk)}?")
            for i, ip in enumerate(found_ips):
                print(f"    {i + 1}. {ip}")
            choice = input(f"  Choose [1-{len(found_ips)}]: ").strip()
            try:
                ip = found_ips[int(choice) - 1]
            except (ValueError, IndexError):
                print("  Skipping.")
                continue

        device["lan_ip"] = ip

        # Fetch and cache auth key
        try:
            print(f"  Fetching encryption key for {dk}...", end="", flush=True)
            auth_key = get_auth_key(token, region, device["product_key"], dk)
            device["auth_key"] = auth_key
            print(f" ✅")
        except Exception as e:
            print(f" ❌ ({e})")
            print("  Local monitoring will fetch the key on next startup (requires internet).")

    print("  LAN configuration complete!")
    return devices


def setup_wizard():
    print("\n🔋 Pecron Monitor Setup\n")

    email = input("Pecron account email: ").strip()
    password = input("Pecron account password: ").strip()

    print("\nRegions:")
    print("  na — North America")
    print("  eu — Europe")
    print("  cn — China")
    region = input("Region [na]: ").strip().lower() or "na"
    if region not in REGIONS:
        print(f"Invalid region '{region}', using 'na'")
        region = "na"

    print("\nTesting login...")
    try:
        token_data = login(email, password, REGIONS[region])
        print(f"✅ Login successful (uid: {token_data['uid']})")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return

    # Try to discover devices from account first (most reliable — gives correct pk/dk)
    print("\n--- Device Setup ---")
    account_devices = get_user_devices(token_data["token"], REGIONS[region])
    devices = []
    use_manual = False

    if account_devices:
        print(f"Found {len(account_devices)} device(s) on your account:\n")
        for i, d in enumerate(account_devices, 1):
            print(f"  {i}. {d['name']}  (dk={d['device_key']})")
        print(f"  {len(account_devices) + 1}. Skip — enter device key manually instead")
        print("")
        default_sel = ",".join(str(i + 1) for i in range(len(account_devices)))
        choice = input(f"Select devices to monitor (e.g. 1 or 1,2) [{default_sel}]: ").strip() or default_sel

        for c in choice.split(","):
            c = c.strip()
            try:
                idx = int(c) - 1
                if idx == len(account_devices):
                    use_manual = True
                elif 0 <= idx < len(account_devices):
                    d = account_devices[idx]
                    devices.append(d)
                    print(f"  ✅ Added: {d['name']} ({d['device_key']})")
            except ValueError:
                pass
    else:
        print("Could not auto-discover devices from your account.")
        use_manual = True

    if use_manual or not devices:
        catalog = get_product_catalog(token_data["token"], REGIONS[region])
        print("\nManual device entry:")
        print("You need your device key (MAC address). Find it in the Pecron app:")
        print("  Device → Settings (⚙️) → Device Info → Device Key")
        print("  It looks like: AABBCCDDEEFF (12 hex characters)")
        print("")
        print("  (Some app versions label this 'Device Code' instead of 'Device Key' — same thing)")
        print("")

    while use_manual:
        dk = input("Device Key (or press Enter to finish): ").strip().upper()
        if not dk:
            break

        print("\nHow would you like to identify your product?")
        print("  1. Auto-detect (scan all models) [default]")
        print("  2. Select from list")
        method = input("Choose [1]: ").strip() or "1"

        if method == "2":
            # Manual selection from catalog
            sorted_products = sorted(catalog.items(), key=lambda x: x[1])
            print("\nAvailable models:")
            for i, (pk, name) in enumerate(sorted_products, 1):
                print(f"  {i:2d}. {name}  (pk={pk})")
            choice = input(f"Select [1-{len(sorted_products)}]: ").strip()
            try:
                idx = int(choice) - 1
                pk, name = sorted_products[idx]
                print(f"  Verifying {name} with key {dk}...", end="", flush=True)
                info = verify_device(token_data["token"], REGIONS[region], pk, dk)
                if info:
                    api_name = info.get("productName", name)
                    print(f"\r  ✅ Verified: {api_name} ({dk})")
                    devices.append({"product_key": pk, "device_key": dk, "name": api_name})
                else:
                    print(f"\r  ❌ Device {dk} not found under {name}.")
                    print("  Try auto-detect, or double-check your device key.")
            except (ValueError, IndexError):
                print("  Invalid selection.")
        else:
            # Auto-detect — try all matching product keys
            matches = []
            print("  Scanning all models...", end="", flush=True)
            for pk, name in catalog.items():
                info = verify_device(token_data["token"], REGIONS[region], pk, dk)
                if info:
                    api_name = info.get("productName", name)
                    matches.append({"pk": pk, "name": api_name, "info": info})

            if len(matches) == 1:
                m = matches[0]
                print(f"\r  ✅ Found: {m['name']} ({dk})")
                devices.append({"product_key": m["pk"], "device_key": dk, "name": m["name"]})
            elif len(matches) > 1:
                print(f"\r  Found {len(matches)} matching product entries:")
                for i, m in enumerate(matches, 1):
                    print(f"    {i}. {m['name']}  (pk={m['pk']})")
                print("  ℹ️  Multiple product keys match your device. If monitoring shows")
                print("     'device is not bound', try --setup again and select a different one.")
                choice = input(f"  Select [1-{len(matches)}] (default=1): ").strip() or "1"
                try:
                    idx = int(choice) - 1
                    m = matches[idx]
                except (ValueError, IndexError):
                    m = matches[0]
                print(f"  ✅ Using: {m['name']} (pk={m['pk']})")
                devices.append({"product_key": m["pk"], "device_key": dk, "name": m["name"]})
            else:
                print(f"\r  ❌ Device {dk} not found. Check the key and try again.")

    if not devices:
        print("⚠️  No devices added. You can add them manually to config.yaml later.")

    # Fetch TSL for each device and cache it
    print("\n--- Fetching Device Metadata ---")
    for d in devices:
        pk = d["product_key"]
        dk = d["device_key"]
        print(f"  Fetching TSL (controls metadata) for {d.get('name', dk)}...", end="", flush=True)
        try:
            tsl = get_product_tsl(token_data["token"], REGIONS[region], pk)
            if tsl:
                d["tsl_cache"] = tsl
                print(f" ✅ ({len(tsl)} properties)")
            else:
                print(" ⚠️  Using defaults")
        except Exception as e:
            print(f" ❌ ({e})")

    # --- Local / BLE Discovery (optional) ---
    if (HAS_LOCAL or HAS_BLE) and devices:
        print("\n--- Local Monitoring (optional) ---")
        print("Monitor your Pecron without internet using WiFi TCP or Bluetooth.\n")
        print("  WiFi TCP — device and computer on same network (faster)")
        print("  Bluetooth — no network needed, ~30ft range (great for vanlife)\n")

        if HAS_LOCAL:
            do_lan = input("Scan for devices on WiFi LAN? [Y/n]: ").strip().lower() != "n"
            if do_lan:
                devices = _setup_lan_discovery(devices, token_data["token"], REGIONS[region])

            # Manual IP entry for devices that weren't found or skipped
            print("\n--- Manual LAN IP Entry (optional) ---")
            for d in devices:
                if d.get("lan_ip"):
                    continue  # Already configured
                manual = input(f"  Enter LAN IP for {d.get('name', d['device_key'])} (or press Enter to skip): ").strip()
                if manual:
                    d["lan_ip"] = manual
                    # Fetch auth key if not already cached
                    if not d.get("auth_key"):
                        try:
                            print(f"  Fetching encryption key...", end="", flush=True)
                            auth_key = get_auth_key(token_data["token"], REGIONS[region],
                                                   d["product_key"], d["device_key"])
                            d["auth_key"] = auth_key
                            print(f" ✅")
                        except Exception as e:
                            print(f" ❌ ({e})")

        if HAS_BLE:
            do_ble = input("Scan for devices via Bluetooth? [Y/n]: ").strip().lower() != "n"
            if do_ble:
                print("  Scanning for Pecron BLE devices...")
                found = scan_ble_devices(timeout=10.0)
                if found:
                    for addr, name in found:
                        print(f"  Found: {name} @ {addr}")
                        # Match to configured device by suffix
                        suffix = name.split("_")[-1] if "_" in name else ""
                        for d in devices:
                            if d["device_key"].upper().endswith(suffix):
                                d["ble_address"] = addr
                                d["ble"] = True
                                print(f"    → Matched to {d.get('name', d['device_key'])}")
                else:
                    print("  No Pecron BLE devices found nearby.")
                    print("  Make sure your Pecron is powered on and Bluetooth is enabled.")

    poll = input("\nPoll interval in seconds [60]: ").strip() or "60"
    threshold = input("Low battery alert threshold % [20]: ").strip() or "20"

    print("\n--- Telegram Alerts (optional) ---")
    tg_enabled = input("Enable Telegram alerts? [y/N]: ").strip().lower() == "y"
    tg_token = tg_chat = ""
    if tg_enabled:
        tg_token = input("Bot token: ").strip()
        tg_chat = input("Chat ID: ").strip()

    print("\n--- Home Assistant (optional) ---")
    ha_enabled = input("Enable Home Assistant MQTT bridge? [y/N]: ").strip().lower() == "y"
    ha_host = ha_user = ha_pw = ""
    if ha_enabled:
        ha_host = input("HA MQTT broker host [localhost]: ").strip() or "localhost"
        ha_user = input("HA MQTT username (optional): ").strip()
        ha_pw = input("HA MQTT password (optional): ").strip()

    config = {
        "email": email, "password": password, "region": region,
        "devices": devices, "poll_interval": int(poll),
        "alerts": {
            "low_battery_percent": int(threshold), "cooldown_minutes": 30,
            "telegram": {"enabled": tg_enabled, "bot_token": tg_token, "chat_id": tg_chat},
            "ntfy": {"enabled": False, "url": ""},
            "webhook": {"enabled": False, "url": ""},
        },
        "homeassistant": {
            "enabled": ha_enabled, "mqtt_host": ha_host or "localhost",
            "mqtt_port": 1883, "mqtt_user": ha_user, "mqtt_password": ha_pw,
            "discovery_prefix": "homeassistant",
        },
        "rules": [
            {
                "name": "Low battery — turn off AC",
                "condition": {"battery_below": 10},
                "action": {"set_ac": False},
                "cooldown_minutes": 30,
                "_comment": "Example rule. Edit or remove as needed.",
            },
        ],
    }

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\n✅ Config saved to {CONFIG_PATH}")
    print("Run 'python pecron_monitor.py' to start monitoring!")
    print("Run 'python pecron_monitor.py --ac on' to test AC control!")


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Pecron Battery Monitor & Controller")
    parser.add_argument("--version", action="version", version=f"pecron-monitor {__version__}")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--local", action="store_true",
                        help="Run in offline/local-only mode (no cloud, uses cached config)")
    parser.add_argument("--status", action="store_true", help="One-shot status check")
    parser.add_argument("--ac", choices=["on", "off"], help="Turn AC output on/off")
    parser.add_argument("--dc", choices=["on", "off"], help="Turn DC output on/off")
    parser.add_argument("--homeassistant", action="store_true", help="Enable HA MQTT bridge")
    parser.add_argument("--raw", action="store_true", help="Dump raw JSON data from device")
    parser.add_argument("--controls", action="store_true", help="List available controls from TSL")
    parser.add_argument("--control", nargs=2, metavar=("CODE", "VALUE"),
                        help="Set any control: --control ac_switch_hm true")
    parser.add_argument("--diagnose", action="store_true",
                        help="Run diagnostics: verify device binding, show MQTT topics, wait for data")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="Config file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    if args.setup:
        setup_wizard()
        return

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found at {config_path}")
        print("Run 'python pecron_monitor.py --setup' to create it.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    monitor = PecronMonitor(config)

    def _signal_handler(sig, frame):
        monitor.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.controls:
        # List available controls for all configured devices
        token_data = login(config["email"], config["password"], REGIONS[config["region"]])
        catalog = get_product_catalog(token_data["token"], REGIONS[config["region"]])
        for d in config.get("devices", []):
            pk, dk = d["product_key"], d["device_key"]
            name = catalog.get(pk, d.get("name", "Unknown"))
            tsl = get_product_tsl(token_data["token"], REGIONS[config["region"]], pk)
            print(f"\n{name} ({dk}):")
            if not tsl:
                print("  (TSL not available — using defaults)")
                tsl = DEFAULT_CONTROLS
            for code, info in sorted(tsl.items(), key=lambda x: x[1]["id"]):
                rw = "RW" if "W" in info.get("access", "R").upper() else "RO"
                print(f"  id={info['id']:3d}  {rw}  {info.get('type','?'):6s}  {code}  — {info.get('desc', '')}")
        return

    if args.raw:
        monitor = PecronMonitor(config)
        monitor.authenticate()
        monitor.connect_mqtt()
        time.sleep(3)
        monitor._request_status()
        time.sleep(5)
        for dk, kv in monitor.latest_data.items():
            print(json.dumps({dk: kv}, indent=2, default=str))
        if not monitor.latest_data:
            print("No data received — device may be offline.")
        monitor.mqtt_client.loop_stop()
        monitor.mqtt_client.disconnect()
        return

    if args.control:
        code, val = args.control
        # Parse value
        if val.lower() in ("true", "on", "1"):
            parsed_val = True
        elif val.lower() in ("false", "off", "0"):
            parsed_val = False
        else:
            try:
                parsed_val = int(val)
            except ValueError:
                print(f"Invalid value: {val}")
                sys.exit(1)
        monitor = PecronMonitor(config)
        monitor.authenticate()
        monitor.connect_mqtt()
        time.sleep(3)
        for device in monitor.devices:
            monitor.send_control(device["device_key"], code, parsed_val)
        time.sleep(3)
        monitor._request_status()
        time.sleep(5)
        for dk, kv in monitor.latest_data.items():
            print(f"Device {dk}: sent {code}={parsed_val}")
        monitor.mqtt_client.loop_stop()
        monitor.mqtt_client.disconnect()
        return

    if args.diagnose:
        print("\n🔍 Pecron Monitor Diagnostics\n")
        region = REGIONS[config["region"]]

        # Step 1: Auth
        print("1. Authentication...")
        try:
            token_data = login(config["email"], config["password"], region)
            print(f"   ✅ Logged in (uid: {token_data['uid']})")
        except Exception as e:
            print(f"   ❌ Login failed: {e}")
            sys.exit(1)

        # Step 2: Product catalog
        print("\n2. Product catalog...")
        catalog = get_product_catalog(token_data["token"], region)
        print(f"   Found {len(catalog)} products in catalog")

        # Step 3: Device verification
        print("\n3. Device verification...")
        for d in config.get("devices", []):
            pk, dk = d["product_key"], d["device_key"]
            config_name = d.get("name", "Unknown")
            catalog_name = catalog.get(pk, "NOT IN CATALOG")
            print(f"\n   Device: {config_name}")
            print(f"   Config product_key: {pk}")
            print(f"   Config device_key:  {dk}")
            print(f"   Catalog name for pk: {catalog_name}")

            info = verify_device(token_data["token"], region, pk, dk)
            if info:
                api_name = info.get("productName", "?")
                print(f"   ✅ Device verified — API says: {api_name}")
                if api_name != config_name:
                    print(f"   ⚠️  Name mismatch: config='{config_name}' vs API='{api_name}'")
                    print(f"      This is cosmetic — the API controls the name shown.")

                # Show binding info
                for key in ["deviceKey", "productKey", "deviceName", "mac", "online"]:
                    if key in info:
                        print(f"   {key}: {info[key]}")
            else:
                print(f"   ❌ Device NOT found with pk={pk} dk={dk}")
                print(f"\n   Searching all products for dk={dk}...")
                found = False
                for cat_pk, cat_name in catalog.items():
                    alt_info = verify_device(token_data["token"], region, cat_pk, dk)
                    if alt_info:
                        print(f"   ✅ Found under: {cat_name} (pk={cat_pk})")
                        print(f"   → Update your config.yaml: product_key: \"{cat_pk}\"")
                        found = True
                        break
                if not found:
                    print(f"   ❌ Device key {dk} not found under ANY product.")
                    print(f"   ⚠️  Double-check your device key from the Pecron app (Device Info → Device Key or Device Code).")
                    print(f"      Should be 12 hex characters (MAC address) like AABBCCDDEEFF")

            # TSL
            tsl = get_product_tsl(token_data["token"], region, pk)
            if tsl:
                rw_count = sum(1 for v in tsl.values() if "W" in v.get("access", "R").upper())
                print(f"   TSL: {len(tsl)} properties ({rw_count} writable)")
            else:
                print(f"   ⚠️  TSL not available for pk={pk}")

        # Step 4: MQTT test
        print("\n4. MQTT connectivity test...")
        print("   Connecting and waiting 15 seconds for data...\n")
        monitor = PecronMonitor(config)
        monitor.authenticate()
        monitor.connect_mqtt()
        time.sleep(3)
        monitor._request_status()

        for i in range(12):
            time.sleep(1)
            if monitor.latest_data:
                break
            if i % 3 == 2:
                print(f"   Waiting... ({i+1}s)")

        if monitor.latest_data:
            print("   ✅ Data received!")
            for dk, kv in monitor.latest_data.items():
                print(f"\n   Device {dk}: {len(kv)} data fields")
                battery = _get_kv(kv, SENSOR_FIELDS["battery_percent"])
                if battery is not None:
                    print(f"   Battery: {battery}%")
                else:
                    print(f"   ⚠️  Battery field not found in response")
                    print(f"   Raw top-level keys: {list(kv.keys())}")
        else:
            print("   ❌ No data received after 15 seconds")
            print("\n   Possible causes:")
            print("   • Device is offline (check Pecron app)")
            print("   • Wrong device_key (used 'Device Code' instead of 'Device Key'?)")
            print("   • Wrong product_key (try --setup to auto-detect)")
            print("   • Device WiFi module is sleeping (open Pecron app to wake it)")
            print("\n   Run with -v for detailed MQTT debug logs:")
            print(f"   python3 pecron_monitor.py --diagnose -v")

        monitor.mqtt_client.loop_stop()
        monitor.mqtt_client.disconnect()
        print("\n✅ Diagnostics complete")
        return

    if args.ac is not None or args.dc is not None:
        monitor.one_shot_command(
            ac=(args.ac == "on") if args.ac else None,
            dc=(args.dc == "on") if args.dc else None,
            force_offline=args.local,
        )
    elif args.status:
        monitor.status_once(force_offline=args.local)
    else:
        monitor.run(
            enable_ha=args.homeassistant or config.get("homeassistant", {}).get("enabled", False),
            force_offline=args.local,
        )


if __name__ == "__main__":
    main()
