"""
Main monitoring logic for pecron-monitor.

Contains the PecronMonitor class which orchestrates cloud authentication,
MQTT connection, local transport management, and data processing.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from helpers import _truthy, _get_kv, _get_kv_single
from constants import REGIONS, DEFAULT_CONTROLS, SENSOR_FIELDS
from cloud_api import (
    login, resolve_devices, get_device_properties_rest
)
from protocol import build_ttlv_read, build_ttlv_write_bool, build_ttlv_write_enum

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

log = logging.getLogger("pecron")


class PecronMonitor:
    def __init__(self, config: dict, no_ble: bool = False):
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
        self.no_ble = no_ble  # Skip BLE transport entirely
        self._local_data_keys = set()  # Track which device_keys got local data this polling cycle

        # Automation rules
        self.rules = config.get("rules", [])
        
        # TCP reconnection tracking (prevent cascading E3800LFP lockout)
        self._local_connect_failures = {}  # device_key → consecutive failure count
        self._last_connect_attempt = {}    # device_key → timestamp of last attempt

    def _next_packet_id(self) -> int:
        self._packet_id = (self._packet_id + 1) % 65535
        return self._packet_id

    def _merge_device_data(self, device_key: str, new_kv: dict):
        """Merge new data into existing device data, preserving non-zero values.

        E3800LFP firmware sends INCOMPLETE packets — one has voltage, another has
        battery_percentage, another has power data. We must merge ALL packets
        instead of overwriting, or we lose voltage/temp/power data.

        Args:
            device_key: Device key to merge data for
            new_kv: New data dict to merge in
        """
        if device_key not in self.latest_data:
            self.latest_data[device_key] = {}
        existing = self.latest_data[device_key]

        for key, value in new_kv.items():
            # Always update if key is new
            if key not in existing:
                existing[key] = value
                continue

            # For nested dicts (like host_packet_data_jdb), merge recursively
            if isinstance(value, dict) and isinstance(existing.get(key), dict):
                for sub_k, sub_v in value.items():
                    # Only overwrite if new value is meaningful (non-zero/non-empty/non-None)
                    if sub_v is not None and sub_v != 0 and sub_v != "":
                        existing[key][sub_k] = sub_v
                    elif sub_k not in existing[key]:
                        # If sub-key doesn't exist yet, set it even if zero
                        existing[key][sub_k] = sub_v
                continue

            # For arrays (like charging_pack_data_jdb), always update
            if isinstance(value, list):
                existing[key] = value
                continue

            # Don't overwrite good data with zero/empty/None
            # This preserves voltage from packet 1 when packet 2 only has battery_percentage
            if value is None or value == "" or (isinstance(value, (int, float)) and value == 0):
                continue  # Keep existing non-zero value

            # Update with new value
            existing[key] = value

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
                self._setup_local_transports()
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
            self._setup_local_transports()

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

        # Set up local transports (TCP + BLE)
        if self.no_ble:
            log.info("BLE disabled (--no-ble flag)")
        self._setup_local_transports()

    def _setup_local_transports(self):
        """Set up local TCP and BLE transports for devices with lan_ip/ble in config."""
        configured = {d.get("device_key"): d for d in self.config.get("devices", [])}

        # Auto-discovery: find devices on LAN if they have auth_key but missing/unreliable lan_ip
        devices_to_discover = []
        for dk, cfg in configured.items():
            if cfg.get("auth_key") and (not cfg.get("lan_ip") or cfg.get("auto_discover", False)):
                devices_to_discover.append({
                    "device_key": dk,
                    "auth_key": cfg["auth_key"]
                })

        if devices_to_discover and HAS_LOCAL:
            try:
                from lan_scan import discover_devices
                discovered = discover_devices(devices_to_discover, timeout=0.5)
                # Update configured IPs with discovered ones
                for dk, ip in discovered.items():
                    if dk in configured:
                        old_ip = configured[dk].get("lan_ip")
                        configured[dk]["lan_ip"] = ip
                        if old_ip != ip:
                            log.info("Updated %s IP: %s → %s", dk, old_ip or "(none)", ip)
            except Exception as e:
                log.warning("Auto-discovery failed: %s", e)

        if HAS_LOCAL:
            for device in self.devices:
                dk = device["device_key"]
                if dk in self.local_transports:
                    continue  # Already set up
                cfg = configured.get(dk, {})
                lan_ip = cfg.get("lan_ip")
                if not lan_ip:
                    continue
                try:
                    auth_key = cfg.get("auth_key")
                    if not auth_key:
                        if self.token_data:
                            log.info("Fetching auth key for %s...", dk)
                            auth_key = get_auth_key(
                                self.token_data["token"], self.region,
                                device["product_key"], dk
                            )
                            log.info("Got auth key for %s (cache it in config.yaml as auth_key)", dk)
                        else:
                            log.warning("No auth key for %s and no cloud token to fetch one", dk)
                            continue
                    self.local_transports[dk] = LocalTransport(lan_ip, auth_key)
                    log.info("Local transport configured for %s @ %s", dk, lan_ip)
                except Exception as e:
                    log.warning("Failed to set up local transport for %s: %s", dk, e)

        if not self.no_ble and HAS_BLE:
            for device in self.devices:
                dk = device["device_key"]
                if dk in self.ble_transports:
                    continue
                cfg = configured.get(dk, {})
                if cfg.get("ble") is False:
                    continue
                ble_addr = cfg.get("ble_address")
                ble_enabled = cfg.get("ble", False)
                if not ble_addr and not ble_enabled:
                    continue
                try:
                    auth_key = cfg.get("auth_key")
                    if not auth_key and dk in self.local_transports:
                        auth_key = self.local_transports[dk].auth_key_b64
                    if not auth_key and self.token_data:
                        log.info("Fetching auth key for %s (BLE)...", dk)
                        auth_key = get_auth_key(
                            self.token_data["token"], self.region,
                            device["product_key"], dk
                        )
                    if auth_key:
                        self.ble_transports[dk] = BLETransport(
                            auth_key, device_address=ble_addr, device_key=dk
                        )
                        log.info("BLE transport configured for %s%s", dk,
                                 f" @ {ble_addr}" if ble_addr else " (will scan)")
                except Exception as e:
                    log.warning("Failed to set up BLE transport for %s: %s", dk, e)

    def _rediscover_device(self, device_key: str) -> bool:
        """Re-discover a single device on the LAN (triggered on connection failure).

        Args:
            device_key: Device key to re-discover

        Returns:
            True if device was found at a new IP, False otherwise
        """
        if not HAS_LOCAL:
            return False

        configured = {d.get("device_key"): d for d in self.config.get("devices", [])}
        cfg = configured.get(device_key)
        if not cfg or not cfg.get("auth_key"):
            return False

        # Skip re-discovery if lan_ip is explicitly configured (pinned IP)
        if cfg.get("lan_ip"):
            log.debug("Skipping re-discovery for %s (lan_ip is pinned in config)", device_key)
            return False

        log.info("Re-discovering device %s (connection lost)...", device_key)
        try:
            from lan_scan import discover_devices
            discovered = discover_devices([{
                "device_key": device_key,
                "auth_key": cfg["auth_key"]
            }], timeout=0.5)

            if device_key in discovered:
                new_ip = discovered[device_key]
                old_ip = cfg.get("lan_ip")
                if new_ip != old_ip:
                    log.info("✅ Re-discovered %s at new IP: %s → %s", device_key, old_ip or "(none)", new_ip)
                    # Update config and transport
                    cfg["lan_ip"] = new_ip
                    # Re-create transport with new IP
                    from local_transport import LocalTransport
                    self.local_transports[device_key] = LocalTransport(new_ip, cfg["auth_key"])
                    return True
                else:
                    log.debug("Device %s still at same IP %s", device_key, new_ip)
                    return False
            else:
                log.warning("Could not re-discover device %s", device_key)
                return False
        except Exception as e:
            log.warning("Re-discovery failed for %s: %s", device_key, e)
            return False

    def _connect_local(self, device_key: str) -> bool:
        """Try to connect local transport for a device.

        The Pecron device closes the TCP socket after each response,
        so we reconnect fresh before every read — this is normal behavior.
        """
        lt = self.local_transports.get(device_key)
        if not lt:
            return False

        # E3800LFP connection cooldown: prevent hammering device during lockout
        # Only skip if the PREVIOUS attempt FAILED (not on every attempt)
        now = time.time()
        last_attempt = self._last_connect_attempt.get(device_key, 0)
        failure_count = self._local_connect_failures.get(device_key, 0)

        # Only apply cooldown if we had a recent failure
        if failure_count > 0 and now - last_attempt < 1.0:
            # Skip connection attempt if we failed less than 1 second ago
            log.debug("Skipping connect for %s (cooldown: %.1fs since last failure)",
                      device_key, now - last_attempt)
            return False

        self._last_connect_attempt[device_key] = now
        
        try:
            connected = lt.connect()
            if connected:
                # Reset failure counter on successful connection
                self._local_connect_failures[device_key] = 0
            else:
                # Increment failure counter
                self._local_connect_failures[device_key] = \
                    self._local_connect_failures.get(device_key, 0) + 1
            return connected
        except Exception as e:
            log.debug("Local connect failed for %s: %s", device_key, e)
            # Increment failure counter on exception
            self._local_connect_failures[device_key] = \
                self._local_connect_failures.get(device_key, 0) + 1
            return False

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
                # Always merge MQTT data with existing local data
                # This is essential for E3800LFP which sends incomplete local TCP packets
                # (battery % in one, voltage/temp/power in MQTT cloud data)
                if device_key in self._local_data_keys:
                    log.debug("Merging CLOUD MQTT data with existing local data for %s", device_key)
                else:
                    log.debug("Processing CLOUD MQTT data for %s", device_key)
                self._merge_device_data(device_key, kv)
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
        # Fix up kv dict for local transports (LOCAL TCP/BLE):
        # Device firmware doesn't compute these fields — they're computed server-side by cloud
        if source in ("LOCAL TCP", "BLE"):
            # Fix battery_percentage: use host_packet_electric_percentage if top-level is 0
            if kv.get("battery_percentage") == 0:
                host_pct = _get_kv_single(kv, ("host_packet_data_jdb", "host_packet_electric_percentage"))
                if host_pct is not None and host_pct > 0:
                    kv["battery_percentage"] = host_pct

        # Fix EP3000 charging_pack_battery field swap (applies to ALL sources)
        # Some devices report battery % in charging_pack_status instead of charging_pack_battery
        packs = kv.get("charging_pack_data_jdb", [])
        if isinstance(packs, list):
            for pack in packs:
                try:
                    pack_battery = int(float(pack.get("charging_pack_battery", 0)))
                    pack_status = int(float(pack.get("charging_pack_status", 0)))
                except (ValueError, TypeError):
                    continue
                # If battery is 0 and status looks like a percentage (>4), swap them
                # Status enum: 0=no charge, 1=cascade charging, 2=balance no charge,
                #              3=balanced charging, 4=no connection — NOT percentages
                if pack_battery == 0 and 5 <= pack_status <= 100:
                    pack["charging_pack_battery"] = pack_status
                    pack["charging_pack_status"] = 0
                    log.debug("Swapped charging_pack fields: battery was 0, using status=%d%%", pack_status)

        battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
        voltage = float(_get_kv(kv, SENSOR_FIELDS["voltage"], 0))
        temp = int(_get_kv(kv, SENSOR_FIELDS["temperature"], 0))
        total_in = int(_get_kv(kv, SENSOR_FIELDS["total_input_power"], 0))
        total_out = int(_get_kv(kv, SENSOR_FIELDS["total_output_power"], 0))
        remain = int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0))

        # Some models (F3000LFP) don't report total_input/output_power at top level
        # over local TCP — compute from AC+DC components as fallback
        if total_in == 0:
            ac_in = int(_get_kv(kv, SENSOR_FIELDS["ac_input_power"], 0))
            dc_in = int(_get_kv(kv, SENSOR_FIELDS["dc_input_power"], 0))
            if ac_in + dc_in > 0:
                total_in = ac_in + dc_in
        if total_out == 0:
            ac_out = int(_get_kv(kv, SENSOR_FIELDS["ac_output_power"], 0))
            dc_out = int(_get_kv(kv, SENSOR_FIELDS["dc_output_power"], 0))
            if ac_out + dc_out > 0:
                total_out = ac_out + dc_out

        # Fix remain_time: local TCP returns unreliable values
        # If remain_time is suspiciously low while battery is high, mark it as unreliable
        if source in ("LOCAL TCP", "BLE") and remain <= 5 and battery_pct > 50:
            remain = -1  # Mark as invalid

        # Skip processing if data is clearly invalid (no real reading)
        if battery_pct < 0 and voltage == 0 and total_in == 0 and total_out == 0:
            log.debug("Skipping invalid/empty data for %s (battery=%d%%, voltage=%.1fV)",
                      device_key, battery_pct, voltage)
            return

        # Track data source — prefer local transports over cloud
        # If we already have a local source, don't let cloud overwrite it
        # (cloud MQTT fires asynchronously and can arrive after local TCP)
        existing_source = self.data_sources.get(device_key)
        LOCAL_SOURCES = ("LOCAL TCP", "BLE")
        if existing_source in LOCAL_SOURCES and source not in LOCAL_SOURCES:
            # Keep the local source designation, but still process the data
            pass
        else:
            self.data_sources[device_key] = source

        # Format remain time (handle unreliable values and 65535 sentinel)
        if remain < 0 or remain >= 65535:
            remain_str = "N/A"
        else:
            remain_str = f"{remain // 60}h{remain % 60}m"

        log.info("🔋 %s%% | %.1fV | %d°C | ⚡ In:%dW Out:%dW | ⏱ %s [via %s]",
                 battery_pct, voltage, temp, total_in, total_out,
                 remain_str, source)

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

    def _get_device_name(self, device_key):
        """Get human-readable device name from device_key."""
        for dev in self.devices:
            if dev.get("device_key") == device_key:
                return dev.get("device_name", dev.get("name", device_key))
        return device_key

    def _send_alert(self, device_key, battery_pct, voltage, remain_min):
        device_name = self._get_device_name(device_key)
        msg = (f"⚠️ Pecron Low Battery Alert\n"
               f"Device: {device_name}\n"
               f"Battery: {battery_pct}%\nVoltage: {voltage:.1f}V\n"
               f"Remaining: {remain_min // 60}h {remain_min % 60}m")
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

        # Try TCP/WiFi local transport (reconnect if needed - Pecron closes TCP after each exchange)
        lt = self.local_transports.get(device_key)
        if lt:
            if not lt.connected:
                try:
                    self._connect_local(device_key)
                except Exception as e:
                    log.debug("Local TCP reconnect failed for %s: %s", device_key, e)
            if lt.connected:
                try:
                    if lt.send_control(ctrl["id"], value, ctrl_type):
                        log.info("Sent %s=%s (type=%s) to %s via TCP", control_code, value, ctrl_type, device_key)
                        return True
                except Exception as e:
                    log.warning("TCP control failed: %s", e)

        # Fall back to cloud MQTT
        if self.mqtt_client is None:
            log.debug("Cannot send control %s: no local transport connected and MQTT is unavailable (offline mode?)", control_code)
            return False
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
        # Sanity check: prevent rule triggers on invalid data
        # If battery is -1 or 0 AND voltage is 0, the data is clearly invalid
        voltage = float(_get_kv(kv, SENSOR_FIELDS["voltage"], 0))
        if battery_pct <= 0 and voltage == 0:
            log.debug("Skipping rule evaluation for %s: invalid data (battery=%d%%, voltage=%.1fV)",
                      device_key, battery_pct, voltage)
            return

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

                # Safety check: verify target device has the required controls
                target_device = None
                for dev in self.devices:
                    if dev.get("device_key") == target_dk:
                        target_device = dev
                        break

                if not target_device:
                    log.warning("Rule '%s': target device %s not found, skipping", rule.get("name"), target_dk)
                    continue

                target_controls = target_device.get("controls", {})

                if "set_ac" in action:
                    if "ac_switch_hm" in target_controls:
                        self.set_ac(target_dk, action["set_ac"])
                        log.info("Rule '%s': set AC=%s on %s", rule.get("name"), action["set_ac"], target_dk)
                    else:
                        log.warning("Rule '%s': device %s does not have AC control, skipping action",
                                    rule.get("name"), target_dk)

                if "set_dc" in action:
                    if "dc_switch_hm" in target_controls:
                        self.set_dc(target_dk, action["set_dc"])
                        log.info("Rule '%s': set DC=%s on %s", rule.get("name"), action["set_dc"], target_dk)
                    else:
                        log.warning("Rule '%s': device %s does not have DC control, skipping action",
                                    rule.get("name"), target_dk)

                if "set_ups" in action:
                    if "ups_status_hm" in target_controls:
                        self.set_ups(target_dk, action["set_ups"])
                        log.info("Rule '%s': set UPS=%s on %s", rule.get("name"), action["set_ups"], target_dk)
                    else:
                        log.warning("Rule '%s': device %s does not have UPS control, skipping action",
                                    rule.get("name"), target_dk)

            except Exception as e:
                log.error("Rule evaluation error: %s", e)

    # --- Status request ---

    def _request_status(self):
        # Clear local data keys at start of polling cycle to allow fresh tracking
        self._local_data_keys.clear()

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
                            self._merge_device_data(dk, kv)
                            self._local_data_keys.add(dk)  # Mark as local data
                            self._process_data(dk, kv, source="BLE")
                            continue
                    except Exception as e:
                        log.warning("BLE read failed for %s: %s", dk, e)

            # Try TCP/WiFi local transport
            # Pecron devices close TCP after each response, so always reconnect
            lt = self.local_transports.get(dk)
            if lt:
                connected = self._connect_local(dk)
                if not connected:
                    # Connection failed — check if we should trigger re-discovery
                    failure_count = self._local_connect_failures.get(dk, 0)
                    configured = {d.get("device_key"): d for d in self.config.get("devices", [])}
                    cfg = configured.get(dk)
                    has_pinned_ip = cfg and cfg.get("lan_ip")
                    
                    if has_pinned_ip:
                        # Pinned IP: skip re-discovery, fall through to cloud MQTT
                        log.debug("Local TCP connection failed for %s (pinned IP, failure #%d) — skipping to cloud",
                                  dk, failure_count)
                    elif failure_count >= 5:
                        # Auto-discovered device with 5+ failures: try re-discovery
                        log.debug("Local TCP connection failed for %s (%d consecutive failures), attempting re-discovery...",
                                  dk, failure_count)
                        if self._rediscover_device(dk):
                            # Re-discovered at new IP, try connecting again
                            lt = self.local_transports.get(dk)  # Get updated transport
                            if lt:
                                connected = self._connect_local(dk)
                    else:
                        # Auto-discovered device with <5 failures: skip to cloud
                        log.debug("Local TCP connection failed for %s (auto-discovered, failure #%d) — skipping to cloud",
                                  dk, failure_count)

                if lt.connected:
                    try:
                        kv = lt.read_status()
                        if kv:
                            log.debug("Got status via LOCAL TCP for %s", dk)
                            self._merge_device_data(dk, kv)
                            self._local_data_keys.add(dk)  # Mark as local data
                            self._process_data(dk, kv, source="LOCAL TCP")
                            # Reset failure counter on successful read
                            self._local_connect_failures[dk] = 0
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
                        self._merge_device_data(dk, kv)
                        self._process_data(dk, kv, source="REST API")

    def _token_needs_refresh(self) -> bool:
        if self.offline_mode:
            return False
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
            from ha_bridge import HomeAssistantBridge
            ha_config = self.config.get("homeassistant", {})
            if ha_config.get("enabled") or enable_ha:
                self.ha_bridge = HomeAssistantBridge(ha_config, self.devices)
                self.ha_bridge.command_callback = self._ha_command
                self.ha_bridge.connect()

        poll_interval = self.config.get("poll_interval", 60)
        log.info("Monitoring started (polling every %ds)", poll_interval)

        # Enable high-frequency MQTT reporting on all devices for fast initial data fill.
        # The E3800 sends telemetry in 3 alternating MQTT packet shapes — with normal
        # reporting (~60s interval), it takes ~3 min to see all shapes. High-freq gets
        # a complete picture in ~30 seconds.
        # Only useful when cloud MQTT is connected — skip in offline/local-only mode.
        if self.mqtt_client:
            self._enable_high_freq_reporting()

        time.sleep(3)
        self._request_status()

        last_high_freq_time = time.time()

        try:
            while self._running:
                time.sleep(poll_interval)

                # Re-send high-freq reporting request every 20s (matches app behavior)
                # Only when cloud MQTT is active — pointless in offline/local-only mode
                if self.mqtt_client and time.time() - last_high_freq_time >= 20:
                    self._enable_high_freq_reporting()
                    last_high_freq_time = time.time()

                if self._token_needs_refresh():
                    log.info("Refreshing token...")
                    try:
                        if self.mqtt_client:
                            self.mqtt_client.loop_stop()
                            self.mqtt_client.disconnect()
                    except Exception:
                        pass
                    self.authenticate(force_offline=force_offline)
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

    def _enable_high_freq_reporting(self):
        """Enable high-frequency MQTT reporting on all devices for fast cache warm-up."""
        for d in self.devices:
            dk = d["device_key"]
            try:
                self.send_control(dk, "high_frequency_reporting", 3)
                log.info("Enabled high-freq reporting for %s", dk)
            except Exception as e:
                log.debug("Could not enable high-freq for %s: %s", dk, e)

    def _disable_high_freq_reporting(self):
        """Disable high-frequency reporting after warm-up period."""
        for d in self.devices:
            dk = d["device_key"]
            try:
                self.send_control(dk, "high_frequency_reporting", 0)
                log.info("Disabled high-freq reporting for %s (warm-up complete)", dk)
            except Exception as e:
                log.debug("Could not disable high-freq for %s: %s", dk, e)

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
            # In offline mode, explicitly connect any local transports before sending controls
            for device in self.devices:
                dk = device["device_key"]
                self._connect_local(dk)
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
        # E3800LFP sends data in multiple MQTT packets with different shapes
        # (voltage in one, battery_percentage in another, power in a third)
        # Wait longer to collect all packet shapes for complete data
        log.info("Collecting data (waiting up to 15s for multi-packet devices like E3800)...")
        time.sleep(15)

        for dk, kv in self.latest_data.items():
            remain = int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0))
            source = self.data_sources.get(dk, "UNKNOWN")

            # Compute total power with AC+DC fallback
            total_in = int(_get_kv(kv, SENSOR_FIELDS["total_input_power"], 0))
            total_out = int(_get_kv(kv, SENSOR_FIELDS["total_output_power"], 0))
            if total_in == 0:
                ac_in = int(_get_kv(kv, SENSOR_FIELDS["ac_input_power"], 0))
                dc_in = int(_get_kv(kv, SENSOR_FIELDS["dc_input_power"], 0))
                if ac_in + dc_in > 0:
                    total_in = ac_in + dc_in
            if total_out == 0:
                ac_out = int(_get_kv(kv, SENSOR_FIELDS["ac_output_power"], 0))
                dc_out = int(_get_kv(kv, SENSOR_FIELDS["dc_output_power"], 0))
                if ac_out + dc_out > 0:
                    total_out = ac_out + dc_out

            # Check if remain_time is unreliable (local transports often return bogus values)
            battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
            if source in ("LOCAL TCP", "BLE") and remain <= 5 and battery_pct > 50:
                remain_str = "N/A (unreliable from local)"
            else:
                remain_str = f"{remain // 60}h {remain % 60}m"

            print(f"\n{'=' * 50}")
            print(f"Device: {dk}")
            print(f"Connection: {source}")
            print(f"{'=' * 50}")
            print(f"Battery:       {_get_kv(kv, SENSOR_FIELDS['battery_percent'], '?')}%")
            print(f"Voltage:       {float(_get_kv(kv, SENSOR_FIELDS['voltage'], 0)):.1f}V")
            print(f"Temperature:   {_get_kv(kv, SENSOR_FIELDS['temperature'], '?')}°C")
            print(f"Remaining:     {remain_str}")
            print(f"Total Input:   {total_in}W")
            print(f"Total Output:  {total_out}W")
            print(f"AC Output:     {_get_kv(kv, SENSOR_FIELDS['ac_output_power'], 0)}W @ {_get_kv(kv, SENSOR_FIELDS['ac_output_voltage'], '?')}V")
            print(f"DC Output:     {_get_kv(kv, SENSOR_FIELDS['dc_output_power'], 0)}W")
            print(f"AC Input:      {_get_kv(kv, SENSOR_FIELDS['ac_input_power'], 0)}W")
            print(f"DC Input:      {_get_kv(kv, SENSOR_FIELDS['dc_input_power'], 0)}W")
            print(f"AC Switch:     {'ON' if _get_kv(kv, SENSOR_FIELDS['ac_switch']) else 'OFF'}")
            print(f"DC Switch:     {'ON' if _get_kv(kv, SENSOR_FIELDS['dc_switch']) else 'OFF'}")
            print(f"UPS Mode:      {'ON' if _get_kv(kv, SENSOR_FIELDS['ups_mode']) else 'OFF'}")

            packs = kv.get("charging_pack_data_jdb", [])
            for i, pack in enumerate(packs):
                try:
                    pack_status_val = int(float(pack.get("charging_pack_status", 4)))
                except (ValueError, TypeError):
                    pack_status_val = 4
                if pack_status_val != 4:
                    # Fallback: some devices report battery % in charging_pack_status instead
                    try:
                        pack_battery = int(float(pack.get('charging_pack_battery', 0)))
                    except (ValueError, TypeError):
                        pack_battery = 0
                    if pack_battery == 0 and 5 <= pack_status_val <= 100:
                        pack_battery = pack_status_val
                        log.debug("Using charging_pack_status (%d%%) as battery for pack %d", pack_status_val, i)
                    print(f"Pack {i}:        {pack_battery if pack_battery > 0 else '?'}% "
                          f"{float(pack.get('charging_pack_voltage', 0)):.1f}V")

        if not self.latest_data:
            print("No data received — device may be offline.")

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

    def stop(self):
        self._running = False
