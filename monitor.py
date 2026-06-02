"""
Main monitoring logic for pecron-monitor.

Contains the PecronMonitor class which orchestrates cloud authentication,
MQTT connection, local transport management, and data processing.
"""

import os
import json
import shlex
import subprocess
import tempfile
import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import output_state

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from helpers import _get_kv, _get_kv_single
from typing import Any, Optional
from constants import (
    DEVICE_STATUS_LABELS,
    REGIONS,
    DEFAULT_CONTROLS,
    SENSOR_FIELDS,
    BATTERY_CAPACITY_WH,
    MODEL_BEHAVIOR,
)
from cloud_api import login, resolve_devices, get_device_properties_rest, set_device_property_rest
from protocol import build_ttlv_read, build_ttlv_write_bool, build_ttlv_write_enum

# Local TCP transport (LAN-first, cloud-fallback)
try:
    from local_transport import LocalTransport, get_auth_key

    HAS_LOCAL = True
except ImportError:
    HAS_LOCAL = False

try:
    from local_transport import BLETransport, HAS_BLE
except ImportError:
    HAS_BLE = False

log = logging.getLogger("pecron")

# Cloud polling rate-limit floor.
# Pecron's cloud applies a per-account cap of roughly 1280 polls/day (issue #29,
# verified empirically by @brucehoult: poll_interval=62 trips code 4026 around
# 23:45 UTC, poll_interval=63 makes it through, poll_interval=120 sails through
# clean). The argument for 63 as the hard floor: at poll_interval=60, his
# account ran out of budget at ~23:00 UTC; bumping to 63 stretches the same
# budget to 23 * 63/60 = 24.15 hours, which crosses the 00:00 UTC reset. We
# refuse to start below MIN_POLL_INTERVAL and warn below RECOMMENDED_POLL_INTERVAL.
MIN_POLL_INTERVAL = 63
RECOMMENDED_POLL_INTERVAL = 70


def _validate_poll_interval(poll_interval: int) -> None:
    """Refuse below MIN_POLL_INTERVAL, warn below RECOMMENDED_POLL_INTERVAL."""
    if poll_interval < MIN_POLL_INTERVAL:
        raise ValueError(
            f"poll_interval={poll_interval}s is below the {MIN_POLL_INTERVAL}s floor. "
            f"Pecron's cloud rate-limits per account at roughly 1280 polls/day; "
            f"poll_interval=62 reliably trips code 4026 ('Insufficient resources') "
            f"around 23:45 UTC daily, while {MIN_POLL_INTERVAL}s is the empirical "
            f"minimum that stretches the budget past the 00:00 UTC reset. Raise "
            f"poll_interval to {RECOMMENDED_POLL_INTERVAL} or higher in config.yaml. "
            f"See pecron-monitor issue #29 for the full evidence trail."
        )
    if poll_interval < RECOMMENDED_POLL_INTERVAL:
        log.warning(
            "poll_interval=%ds is below the recommended %ds and may trip cloud rate-limit "
            "code 4026 within ~24h (issue #29). Consider raising to %d.",
            poll_interval,
            RECOMMENDED_POLL_INTERVAL,
            RECOMMENDED_POLL_INTERVAL,
        )


def _validate_poll_interval_for_mode(poll_interval: int, force_offline: bool) -> None:
    """Apply the cloud poll floor only when cloud polling can be used."""
    if force_offline:
        if poll_interval < MIN_POLL_INTERVAL:
            log.info(
                "poll_interval=%ds is below the cloud floor but allowed in local/offline mode",
                poll_interval,
            )
        return
    _validate_poll_interval(poll_interval)


class PecronMonitor:
    def __init__(self, config: dict, no_ble: bool = False, rest_only: bool = False):
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
        self.ble_transports = {}  # device_key → BLETransport
        self.offline_mode = False  # Set to True when running in local-only mode
        self.no_ble = no_ble  # Skip BLE transport entirely
        self.rest_only = rest_only  # If True, disable MQTT and local transports; use REST API only
        self._local_data_keys = set()  # Track which device_keys got local data this polling cycle
        self._last_logged_values = {}  # Track last logged values per device to avoid duplicate logs

        # Automation rules
        self.rules = config.get("rules", [])
        self.rule_state_config = config.get("rule_state", {}) or {}
        self.rule_state = self._load_rule_state()
        self._init_rules_ran = False

        self._mqtt_connect_failures = 0
        self._last_mqtt_rebuild_at = 0.0
        # TCP reconnection tracking (prevent cascading E3800LFP lockout)
        self._local_connect_failures = {}  # device_key → consecutive failure count
        self._last_connect_attempt = {}  # device_key → timestamp of last attempt
        # Option to skip setting up local transports (set by authenticate skip_local)
        self.skip_local_setup = False

        # Cloud recovery state (issue #23). _fell_back_to_offline distinguishes
        # an unplanned fallback (transient DNS/network failure during cloud login)
        # from a user-requested offline run. Only the former should be retried.
        self._fell_back_to_offline = False
        self._last_cloud_retry_at = 0.0

        # Restore-outputs-after-shutdown state (issue #59).
        # We track per-device offline/online wall-clock to gate the restore on
        # "at least N seconds offline" — that filters out network blips that
        # don't represent a real device shutdown. The _restore_threads dict
        # dedupes concurrent worker spawns (online flap re-triggering restore).
        self._last_offline_at: dict[str, float] = {}
        self._last_online_at: dict[str, float] = {}
        self._restore_threads: dict[str, threading.Thread] = {}

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

    def authenticate(self, force_offline: bool = False, skip_local: Optional[bool] = None):
        """Authenticate and set up transports.

        Args:
            force_offline: If True, skip cloud login and use cached config only.
                          Auto-detected when all devices have local credentials.
        """
        # Check if we can run fully offline
        can_offline = self._check_offline_capable()

        # Honor skip_local flag for this monitor instance (only if provided)
        if skip_local is not None:
            self.skip_local_setup = bool(skip_local)

        # If running in REST-only mode, always skip local transports
        if self.rest_only:
            self.skip_local_setup = True

        if force_offline:
            if not can_offline:
                raise RuntimeError(
                    "Cannot run in offline mode: missing required fields.\n"
                    "Each device needs: lan_ip or ble_address, auth_key, product_key, device_key.\n"
                    "Run --setup first to fetch and cache these credentials."
                )
            self.offline_mode = True
            self._fell_back_to_offline = False  # user-requested; no cloud retry
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
                if not self.skip_local_setup:
                    self._setup_local_transports()
                self.offline_mode = False
                self._fell_back_to_offline = False
            except Exception as e:
                log.warning("Cloud login failed (%s), falling back to offline mode", e)
                self.offline_mode = True
                self._fell_back_to_offline = True  # issue #23: retry periodically
                self._last_cloud_retry_at = time.time()
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
            if not self.skip_local_setup:
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

            self.devices.append(
                {
                    "product_key": pk,
                    "device_key": dk,
                    "device_name": name,
                    "product_name": name,
                    "controls": controls,
                }
            )
            log.info("  📦 Loaded from config: %s (pk=%s, dk=%s)", name, pk, dk)

        log.info("Loaded %d device(s) from config", len(self.devices))

        # Set up local transports (TCP + BLE)
        if self.no_ble:
            log.info("BLE disabled (--no-ble flag)")
        if not self.skip_local_setup:
            self._setup_local_transports()

    def _setup_local_transports(self):
        """Set up local TCP and BLE transports for devices with lan_ip/ble in config."""
        configured = {d.get("device_key"): d for d in self.config.get("devices", [])}

        # Auto-discovery: find devices on LAN if they have auth_key but missing/unreliable lan_ip
        devices_to_discover = []
        for dk, cfg in configured.items():
            if cfg.get("auth_key") and (not cfg.get("lan_ip") or cfg.get("auto_discover", False)):
                devices_to_discover.append({"device_key": dk, "auth_key": cfg["auth_key"]})

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
                                self.token_data["token"], self.region, device["product_key"], dk
                            )
                            log.info(
                                "Got auth key for %s (cache it in config.yaml as auth_key)", dk
                            )
                        else:
                            log.warning("No auth key for %s and no cloud token to fetch one", dk)
                            continue
                    self.local_transports[dk] = LocalTransport(
                        lan_ip,
                        auth_key,
                        device_key=dk,
                        controls=device.get("controls", DEFAULT_CONTROLS),
                    )
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
                            self.token_data["token"], self.region, device["product_key"], dk
                        )
                    if auth_key:
                        self.ble_transports[dk] = BLETransport(
                            auth_key,
                            device_address=ble_addr,
                            device_key=dk,
                            controls=device.get("controls", DEFAULT_CONTROLS),
                        )
                        log.info(
                            "BLE transport configured for %s%s",
                            dk,
                            f" @ {ble_addr}" if ble_addr else " (will scan)",
                        )
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

            discovered = discover_devices(
                [{"device_key": device_key, "auth_key": cfg["auth_key"]}], timeout=0.5
            )

            if device_key in discovered:
                new_ip = discovered[device_key]
                old_ip = cfg.get("lan_ip")
                if new_ip != old_ip:
                    log.info(
                        "✅ Re-discovered %s at new IP: %s → %s",
                        device_key,
                        old_ip or "(none)",
                        new_ip,
                    )
                    # Update config and transport
                    cfg["lan_ip"] = new_ip
                    # Re-create transport with new IP
                    from local_transport import LocalTransport

                    self.local_transports[device_key] = LocalTransport(
                        new_ip,
                        cfg["auth_key"],
                        device_key=device_key,
                        controls=self._find_device(device_key).get("controls", DEFAULT_CONTROLS),
                    )
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
            log.debug(
                "Skipping connect for %s (cooldown: %.1fs since last failure)",
                device_key,
                now - last_attempt,
            )
            return False

        self._last_connect_attempt[device_key] = now

        try:
            connected = lt.connect()
            if connected:
                # Reset failure counter on successful connection
                self._local_connect_failures[device_key] = 0
            else:
                # Increment failure counter
                self._local_connect_failures[device_key] = (
                    self._local_connect_failures.get(device_key, 0) + 1
                )
            return connected
        except Exception as e:
            log.debug("Local connect failed for %s: %s", device_key, e)
            # Increment failure counter on exception
            self._local_connect_failures[device_key] = (
                self._local_connect_failures.get(device_key, 0) + 1
            )
            return False

    def _channel_id(self, device: dict) -> str:
        return f"qd{device['product_key']}{device['device_key']}"

    def _has_telemetry_fields(self, kv: dict) -> bool:
        """Check if data dict contains COMPLETE telemetry fields (not just settings).

        E3600/E3800 local TCP returns ONLY settings fields (14 fields like
        ac_output_voltage_io, ac_output_frequency_io, noastime_io, ac_switch_hm, etc.)
        but NO telemetry (battery_percentage, voltage, power, temperature).

        E3800 might return battery_percentage alone, but without voltage/power/temp,
        so we need to check for host_packet_data_jdb which contains the real telemetry.

        This method checks for key telemetry fields to determine if local data
        should be treated as primary or if we need to rely on MQTT cloud data.

        Args:
            kv: Data dict to check

        Returns:
            True if data contains COMPLETE telemetry fields, False if only settings
        """
        # host_packet_data_jdb is the most reliable indicator - it contains
        # voltage, temperature, and battery % in nested form
        # E1500LFP returns this with full data, E3600/E3800 do not
        if "host_packet_data_jdb" in kv:
            host_data = kv["host_packet_data_jdb"]
            if isinstance(host_data, dict) and host_data:
                # Check if it has actual voltage/temp data (not just empty dict)
                try:
                    has_voltage = float(host_data.get("host_packet_voltage", 0)) > 0
                except (ValueError, TypeError):
                    has_voltage = False
                has_temp = "host_packet_temp" in host_data
                if has_voltage or has_temp:
                    return True

        # Check for power data structures (E1500 has these, E3600/E3800 don't via local TCP)
        power_structures = [
            "ac_data_output_hm",
            "dc_data_output_hm",
            "ac_data_input_hm",
            "dc_data_input_hm",
        ]

        for field in power_structures:
            if field in kv:
                value = kv[field]
                if isinstance(value, dict) and value:
                    return True

        # Check for top-level power fields
        if kv.get("total_input_power", 0) > 0 or kv.get("total_output_power", 0) > 0:
            return True

        # Check for battery_percentage at top level (E3600/E3800 MQTT telemetry packets)
        battery_pct = kv.get("battery_percentage")
        if battery_pct is not None:
            try:
                if int(float(battery_pct)) >= 0:
                    return True
            except (ValueError, TypeError):
                pass

        return False

    def _find_device(self, device_key: str) -> dict:
        for d in self.devices:
            if d["device_key"] == device_key:
                return d
        return {}

    # --- MQTT callbacks ---

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != mqtt.CONNACK_ACCEPTED:
            self._mqtt_connect_failures += 1
            log.error("MQTT connection failed: %s", mqtt.connack_string(rc))
            return
        self._mqtt_connect_failures = 0
        log.info("MQTT connected")
        for device in self.devices:
            cid = self._channel_id(device)
            for suffix in ["bus_", "ack_", "onl_"]:
                topic = f"q/2/d/{cid}/{suffix}"
                client.subscribe(topic, qos=1)
                log.debug("  Subscribed: %s", topic)
            log.info(
                "Subscribed to %s (pk=%s, dk=%s, channel=%s)",
                device["device_name"],
                device["product_key"],
                device["device_key"],
                cid,
            )

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("Non-JSON MQTT message on %s (%d bytes)", msg.topic, len(msg.payload))
            return

        topic_suffix = msg.topic.split("/")[-1]
        device_key = payload.get("deviceKey", "")
        log.debug(
            "MQTT message: topic=%s suffix=%s dk=%s keys=%s",
            msg.topic,
            topic_suffix,
            device_key,
            list(payload.keys()),
        )

        if topic_suffix == "bus_" and "data" in payload:
            kv = payload["data"].get("kv", {})
            log.debug("MQTT kv keys for %s: %s", device_key, list(kv.keys()))
            if kv:
                # Always merge MQTT data with existing local data
                # This is essential for E3600/E3800LFP which:
                # 1. Send incomplete local TCP packets (only settings, no telemetry)
                # 2. Send alternating MQTT packets (one with battery%, another with power)
                # We merge all incoming data, then process from the accumulated state
                if device_key in self._local_data_keys:
                    log.debug("Merging CLOUD MQTT data with existing local data for %s", device_key)
                else:
                    log.debug("Processing CLOUD MQTT data for %s", device_key)

                self._merge_device_data(device_key, kv)

                # Process from the ACCUMULATED data, not just this message
                # This ensures we have the complete picture after multiple partial packets
                accumulated = self.latest_data.get(device_key, kv)
                self._process_data(device_key, accumulated, source="CLOUD MQTT")
            else:
                log.debug("bus_ message with empty kv: %s", list(payload["data"].keys()))
        elif topic_suffix == "onl_" and "data" in payload:
            online = payload["data"].get("value", 0) == 1
            if online:
                self._on_device_online(device_key)
            else:
                self._on_device_offline(device_key)
        elif topic_suffix == "ack_":
            log.debug("ACK received for device %s", device_key)
        elif topic_suffix == "sys_":
            # System messages (responses to our publishes, device online/offline events)
            code = payload.get("code")
            msg_text = payload.get("msg", "")
            msg_type = payload.get("type", "")
            if code == 4007:
                if not hasattr(self, "_4007_warned"):
                    self._4007_warned = True
                    log.warning(
                        "Cloud reported 'device is not bound' (code 4007) during startup/control traffic."
                    )
                    log.warning(
                        "This can mean the wrong product_key is configured, but it can also be a transient or noisy cloud-system message."
                    )
                    log.warning(
                        "If device verification succeeds or telemetry still arrives via MQTT/local/REST, you can usually ignore this warning."
                    )
                    log.warning(
                        "Only treat it as actionable if the warning persists AND the device never produces telemetry."
                    )
                    log.warning(
                        "Then run 'python pecron_monitor.py --diagnose -v' or '--setup' to verify the product_key/device_key pair."
                    )
            elif code == 4026:
                log.warning(
                    "Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type
                )
                if not getattr(self, "_4026_warned", False):
                    self._4026_warned = True
                    configured = self.config.get("poll_interval", RECOMMENDED_POLL_INTERVAL)
                    log.error(
                        "Pecron cloud returned code 4026 ('Insufficient resources'). This is a "
                        "per-account polling rate-limit (~1280 polls/day) — not a Pecron-side "
                        "outage. Current poll_interval=%ds. Raise it in config.yaml (>=%d "
                        "recommended) and restart. The cap resets at 00:00 UTC. See issue #29.",
                        configured,
                        RECOMMENDED_POLL_INTERVAL,
                    )
            elif code and code != 200:
                log.warning(
                    "Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type
                )
            else:
                log.debug(
                    "Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type
                )

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
                host_pct = _get_kv_single(
                    kv, ("host_packet_data_jdb", "host_packet_electric_percentage")
                )
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
                # Also swap in the other direction if needed
                if pack_battery == 0 and 5 <= pack_status <= 100:
                    pack["charging_pack_battery"] = pack_status
                    pack["charging_pack_status"] = 0
                    log.debug(
                        "Swapped charging_pack fields: battery was 0, using status=%d%%",
                        pack_status,
                    )
                elif pack_status == 0 and 5 <= pack_battery <= 100:
                    pack["charging_pack_status"] = pack_battery
                    pack["charging_pack_battery"] = 0
                    log.debug(
                        "Swapped charging_pack fields: status was 0, using battery=%d%% as status",
                        pack_battery,
                    )

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

        # Check if data looks incomplete (E3600/E3800 MQTT sends alternating packets)
        # Don't immediately return — let the data accumulate in latest_data via merge
        # Only skip the status log line to avoid spamming with incomplete readings
        data_incomplete = battery_pct < 0 and voltage == 0 and total_in == 0 and total_out == 0
        if data_incomplete:
            log.debug(
                "Skipping status log for %s (incomplete packet: battery=%d%%, voltage=%.1fV) — data will accumulate",
                device_key,
                battery_pct,
                voltage,
            )
            # Still update HA bridge and check alerts with what we have
            if self.ha_bridge:
                self.ha_bridge.publish_state(device_key, kv)
            return  # Skip status log and automation rules for incomplete data

        # Filter out misleading 0.0V voltage readings when battery_pct is valid
        # E3600/E3800 MQTT sends alternating packets — one with battery%, another with voltage
        # When battery% packet arrives first, voltage hasn't been received yet (shows 0.0V)
        # Skip the status log in this case to avoid misleading "80% | 0.0V" logs
        if voltage == 0 and battery_pct >= 0:
            log.debug(
                "Skipping status log for %s (voltage not yet received: battery=%d%%, voltage=%.1fV) — waiting for voltage packet",
                device_key,
                battery_pct,
                voltage,
            )
            # Still update HA bridge and check alerts with what we have
            if self.ha_bridge:
                self.ha_bridge.publish_state(device_key, kv)
            return  # Skip status log until voltage arrives

        # Issue #60: suppress LOCAL TCP "shutdown-window zero-frame" placeholders.
        # When the inverter is gating off during a low-battery shutdown, local TCP
        # returns a frame with fresh battery_pct (0) and voltage but zeroed power
        # and remain_time. Those zeroes are technically real-time-truth (no current
        # is flowing because the inverter is off) but in HA they clobber the cloud's
        # last-known-good values for the 1-2 minute shutdown window, making "if
        # input < 5W for 10min" automations false-fire. Cloud MQTT continues to
        # arrive concurrently and stays the source of truth for power fields
        # during this transition.
        is_local_shutdown_zero_frame = (
            source in ("LOCAL TCP", "BLE")
            and battery_pct == 0
            and total_in == 0
            and total_out == 0
            and remain <= 0
        )
        if is_local_shutdown_zero_frame:
            log.debug(
                "Skipping %s shutdown-window zero-frame for %s "
                "(battery=0%%, voltage=%.1fV, all power=0) — letting cloud "
                "telemetry stay authoritative for HA during shutdown.",
                source,
                device_key,
                voltage,
            )
            return  # Don't update HA, don't re-fire alerts, don't log status

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

        # Stale data detection: only log when values actually change
        # When high-freq is disabled, data arrives every ~20 min but status is polled more frequently
        # This prevents spamming logs with identical readings on every poll cycle
        current_values = (battery_pct, voltage, temp, total_in, total_out)
        last_values = self._last_logged_values.get(device_key)

        if last_values == current_values:
            log.debug(
                "Skipping status log for %s (values unchanged: %d%%, %.1fV, %d°C, In:%dW, Out:%dW)",
                device_key,
                battery_pct,
                voltage,
                temp,
                total_in,
                total_out,
            )
            # Still update HA bridge and check alerts even with stale data
            if self.ha_bridge:
                self.ha_bridge.publish_state(device_key, kv)
            self._check_alerts(device_key, battery_pct, voltage, remain)
            self._evaluate_rules(device_key, kv, battery_pct)
            return  # Skip status log for unchanged data

        # Update last logged values
        self._last_logged_values[device_key] = current_values

        log.info(
            "🔋 %s%% | %.1fV | %d°C | ⚡ In:%dW Out:%dW | ⏱ %s [via %s]",
            battery_pct,
            voltage,
            temp,
            total_in,
            total_out,
            remain_str,
            source,
        )

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
        msg = (
            f"⚠️ Pecron Low Battery Alert\n"
            f"Device: {device_name}\n"
            f"Battery: {battery_pct}%\nVoltage: {voltage:.1f}V\n"
            f"Remaining: {remain_min // 60}h {remain_min % 60}m"
        )
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
                req = urllib.request.Request(
                    ntfy["url"],
                    data=msg.encode(),
                    headers={"Title": f"Pecron Battery {battery_pct}%"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.error("ntfy alert failed: %s", e)

        wh = alerts.get("webhook", {})
        if wh.get("enabled") and wh.get("url"):
            try:
                payload = json.dumps(
                    {
                        "battery_percent": battery_pct,
                        "voltage": voltage,
                        "remain_minutes": remain_min,
                        "device_key": device_key,
                        "message": msg,
                    }
                ).encode()
                req = urllib.request.Request(
                    wh["url"], data=payload, headers={"Content-Type": "application/json"}
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.error("Webhook alert failed: %s", e)

    # --- Control commands ---

    def send_control(self, device_key: str, control_code: str, value, verify: bool = True):
        """Send a control command. Auto-detects type from TSL (BOOL, ENUM, INT).

        `verify=True` (default) asks the local/BLE transport to read the data
        point back and confirm the device applied the write (issue #46). Pass
        `verify=False` for transient control codes that the device intentionally
        auto-reverts (e.g. `high_frequency_reporting`, see issue #50) so the
        read-back doesn't spuriously log a mismatch warning. Cloud-only
        transports (MQTT/REST) are unaffected -- they have no read-back step.
        """
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
                if ble.send_control(ctrl["id"], value, ctrl_type, verify=verify):
                    log.info(
                        "Sent %s=%s (type=%s) to %s via BLE",
                        control_code,
                        value,
                        ctrl_type,
                        device_key,
                    )
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
                    if lt.send_control(ctrl["id"], value, ctrl_type, verify=verify):
                        log.info(
                            "Sent %s=%s (type=%s) to %s via TCP",
                            control_code,
                            value,
                            ctrl_type,
                            device_key,
                        )
                        return True
                except Exception as e:
                    log.warning("TCP control failed: %s", e)

        # Fall back to cloud transports (MQTT primary, REST fallback)
        # Normal mode: try MQTT first, REST as fallback
        if self.mqtt_client is not None:
            self.mqtt_client.publish(f"q/1/d/{cid}/bus", pkt, qos=1)
            log.info(
                "Sent %s=%s (type=%s) to %s via CLOUD MQTT",
                control_code,
                value,
                ctrl_type,
                device_key,
            )
            return True

        # MQTT not available, try REST API as fallback
        if self.token_data:
            if set_device_property_rest(
                self.token_data["token"],
                self.region,
                device["product_key"],
                device_key,
                {control_code: value},
            ):
                log.info("Sent %s=%s to %s via CLOUD REST API", control_code, value, device_key)
                return True

        log.debug("Cannot send control %s: no cloud transport available", control_code)
        return False

    def _extract_value_by_key(self, obj: Any, key: str):
        """Find first occurrence of a key in nested dict/list structures."""
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = self._extract_value_by_key(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._extract_value_by_key(item, key)
                if found is not None:
                    return found
        return None

    def _normalize_probe_readback(self, value):
        """Normalize readback values for robust integer comparison."""
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("on", "true", "enabled"):
                return 1
            if lowered in ("off", "false", "disabled"):
                return 0
            try:
                return int(float(lowered))
            except ValueError:
                return None
        return None

    def probe_control_values(
        self, device_key: str, control_code: str, min_value: int = 0, max_value: int = 255
    ) -> dict:
        """Probe supported control values from min_value upward with set-then-readback validation.

        For each candidate value:
        1) Send control value
        2) Request status
        3) Read back same control key
        4) Continue while readback equals candidate value

        Returns probe details including the contiguous valid value set.
        """
        device = self._find_device(device_key)
        if not device:
            return {
                "device_key": device_key,
                "control_code": control_code,
                "valid_values": [],
                "stop_value": 0,
                "last_readback": None,
                "reason": "device_not_found",
            }

        controls = device.get("controls", DEFAULT_CONTROLS)
        ctrl = controls.get(control_code)
        if not ctrl:
            return {
                "device_key": device_key,
                "control_code": control_code,
                "valid_values": [],
                "stop_value": 0,
                "last_readback": None,
                "reason": "control_not_found",
            }

        valid_values = []
        stop_value = min_value
        last_readback = None
        reason = "readback_mismatch"

        for candidate in range(min_value, max_value + 1):
            stop_value = candidate

            sent = self.send_control(device_key, control_code, candidate)
            if not sent:
                reason = "send_failed"
                break

            # Allow device to apply state before requesting readback.
            time.sleep(3)
            # Clear only this device's cached reading before fresh readback
            self.latest_data.pop(device_key, None)
            self._request_status()
            time.sleep(1)

            kv = self.latest_data.get(device_key, {})
            raw_readback = self._extract_value_by_key(kv, control_code)
            normalized_readback = self._normalize_probe_readback(raw_readback)
            last_readback = raw_readback

            if normalized_readback != candidate:
                reason = "readback_mismatch"
                break

            valid_values.append(candidate)
        else:
            reason = "max_reached"

        return {
            "device_key": device_key,
            "control_code": control_code,
            "valid_values": valid_values,
            "stop_value": stop_value,
            "last_readback": last_readback,
            "reason": reason,
        }

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

    def _rule_state_path(self) -> Path:
        """Resolve persisted rule-state path."""
        override = os.environ.get("PECRON_RULE_STATE_PATH")
        if override:
            return Path(override)
        configured = self.rule_state_config.get("path") or self.config.get("rule_state_path")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".pecron-monitor-rules.json"

    def _initial_rule_state(self) -> str:
        """Return configured initial rule state."""
        return str(self.rule_state_config.get("initial_state", "default"))

    def _load_rule_state(self) -> str:
        """Load persisted rule state, falling back to the configured initial state."""
        path = self._rule_state_path()
        fallback = self._initial_rule_state()
        if not path.exists():
            return fallback
        try:
            with path.open("r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not load rule state from %s: %s", path, e)
            return fallback
        state = data.get("state") if isinstance(data, dict) else None
        return str(state) if state else fallback

    def _save_rule_state(self) -> None:
        """Persist current rule state atomically."""
        path = self._rule_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".pecron-rules-", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {"state": self.rule_state, "updated_at": datetime.utcnow().isoformat()},
                    f,
                )
                f.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    def _set_rule_state(self, state: str) -> None:
        """Change and persist the single rule engine state."""
        new_state = str(state)
        if new_state == self.rule_state:
            return
        old_state = self.rule_state
        self.rule_state = new_state
        self._save_rule_state()
        log.info("Rule state changed: %s -> %s", old_state, new_state)

    def _rule_state_matches(self, condition: dict) -> bool:
        """Return whether current state satisfies a rule condition's state gate."""
        if "state" in condition:
            return self.rule_state == str(condition["state"])
        states = condition.get("states")
        if states is None:
            return True
        if isinstance(states, str):
            return self.rule_state == states
        return self.rule_state in {str(state) for state in states}

    def _run_rule_command(
        self,
        command,
        *,
        rule: dict,
        action: dict,
        device_key: str,
        target_device_key: str,
        kv: dict,
        battery_pct: int,
    ) -> None:
        """Run a configured external command with rule context on stdin as JSON."""
        if isinstance(command, str):
            argv = shlex.split(command)
        else:
            argv = [str(part) for part in command]
        if not argv:
            raise ValueError("run_command cannot be empty")

        payload = {
            "rule": rule.get("name"),
            "state": self.rule_state,
            "device_key": device_key,
            "target_device_key": target_device_key,
            "battery_percent": battery_pct,
            "voltage": self._extract_voltage(kv),
            "data": kv,
        }
        timeout = float(action.get("timeout_seconds", 30))
        result = subprocess.run(
            argv,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.stdout.strip():
            log.info("Rule '%s' command stdout: %s", rule.get("name"), result.stdout.strip())
        if result.stderr.strip():
            log.warning("Rule '%s' command stderr: %s", rule.get("name"), result.stderr.strip())
        if result.returncode != 0:
            raise RuntimeError(f"command exited with status {result.returncode}: {argv[0]}")

    # Trigger condition keys, ANDed together when more than one is present on a
    # rule (#56). `state`/`states` are separate preconditions handled by
    # _rule_state_matches and are intentionally not in this list.
    _TRIGGER_CONDITION_KEYS = (
        "init",
        "battery_below",
        "battery_above",
        "voltage_below",
        "voltage_above",
        "input_power_below",
        "input_power_above",
        "output_power_below",
        "output_power_above",
        "schedule",
        "schedule_between",
    )

    @staticmethod
    def _in_time_window(now_hm: str, start_hm: str, end_hm: str) -> bool:
        """True if now is within [start, end), supporting windows that wrap past
        midnight (e.g. 22:00-06:00). A zero-width window never matches."""

        def _mins(hm: str) -> int:
            hh, mm = str(hm).split(":")
            return int(hh) * 60 + int(mm)

        try:
            now, start, end = _mins(now_hm), _mins(start_hm), _mins(end_hm)
        except (ValueError, AttributeError):
            log.warning("Invalid HH:MM in schedule_between: %r-%r", start_hm, end_hm)
            return False
        if start == end:
            return False
        if start < end:
            return start <= now < end
        return now >= start or now < end

    def _eval_condition_clause(
        self, key: str, condition: dict, kv: dict, battery_pct: int, init: bool
    ) -> bool:
        """Evaluate one trigger clause. Returns True only if satisfied; missing or
        unevaluable telemetry returns False so the (ANDed) rule does not fire."""
        if key == "init":
            # `init` is a startup trigger only when true. `init: false` must not
            # become an "always fires on normal eval" trigger (#56 review).
            return bool(condition.get("init")) and init
        if key == "battery_below":
            return battery_pct >= 0 and battery_pct <= condition["battery_below"]
        if key == "battery_above":
            return battery_pct >= 0 and battery_pct >= condition["battery_above"]
        if key == "voltage_below":
            v = self._extract_voltage(kv)
            return v is not None and v <= float(condition["voltage_below"])
        if key == "voltage_above":
            v = self._extract_voltage(kv)
            return v is not None and v >= float(condition["voltage_above"])
        if key == "input_power_below":
            p = self._extract_power(kv, "total_input_power", "ac_input_power", "dc_input_power")
            return p is not None and p <= condition["input_power_below"]
        if key == "input_power_above":
            p = self._extract_power(kv, "total_input_power", "ac_input_power", "dc_input_power")
            return p is not None and p >= condition["input_power_above"]
        if key == "output_power_below":
            p = self._extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power")
            return p is not None and p <= condition["output_power_below"]
        if key == "output_power_above":
            p = self._extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power")
            return p is not None and p >= condition["output_power_above"]
        if key == "schedule":
            return datetime.now().strftime("%H:%M") == condition["schedule"]
        if key == "schedule_between":
            window = condition["schedule_between"]
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                log.warning("schedule_between must be [start, end] HH:MM, got %r", window)
                return False
            return self._in_time_window(datetime.now().strftime("%H:%M"), window[0], window[1])
        return False

    def _evaluate_rules(self, device_key: str, kv: dict, battery_pct: int, *, init: bool = False):
        """Evaluate automation rules against current state."""
        # Sanity check: prevent rule triggers on invalid non-init data.
        voltage = self._extract_voltage(kv) or 0
        if not init and battery_pct <= 0 and voltage == 0:
            log.debug(
                "Skipping rule evaluation for %s: invalid data (battery=%d%%, voltage=%.1fV)",
                device_key,
                battery_pct,
                voltage,
            )
            return

        for rule in self.rules:
            if rule.get("device_key") and rule["device_key"] != device_key:
                continue

            try:
                condition = rule.get("condition", {})
                action = rule.get("action", {})

                if not self._rule_state_matches(condition):
                    continue

                # Check condition. `state`/`states` are preconditions handled
                # above, not triggers by themselves. Every trigger key present is
                # ANDed (#56): e.g. voltage_below + output_power_below fires only
                # when both hold. A rule with no trigger key (state gate only)
                # never fires. A clause that can't be evaluated (missing
                # telemetry) returns False, so the rule does not fire.
                present = [k for k in self._TRIGGER_CONDITION_KEYS if k in condition]
                if not present:
                    continue
                if not all(
                    self._eval_condition_clause(k, condition, kv, battery_pct, init)
                    for k in present
                ):
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
                    log.warning(
                        "Rule '%s': target device %s not found, skipping",
                        rule.get("name"),
                        target_dk,
                    )
                    continue

                target_controls = target_device.get("controls", {})

                if "set_ac" in action:
                    if "ac_switch_hm" in target_controls:
                        self.set_ac(target_dk, action["set_ac"])
                        log.info(
                            "Rule '%s': set AC=%s on %s",
                            rule.get("name"),
                            action["set_ac"],
                            target_dk,
                        )
                    else:
                        log.warning(
                            "Rule '%s': device %s does not have AC control, skipping action",
                            rule.get("name"),
                            target_dk,
                        )

                if "set_dc" in action:
                    if "dc_switch_hm" in target_controls:
                        self.set_dc(target_dk, action["set_dc"])
                        log.info(
                            "Rule '%s': set DC=%s on %s",
                            rule.get("name"),
                            action["set_dc"],
                            target_dk,
                        )
                    else:
                        log.warning(
                            "Rule '%s': device %s does not have DC control, skipping action",
                            rule.get("name"),
                            target_dk,
                        )

                if "set_ups" in action:
                    if "ups_status_hm" in target_controls:
                        self.set_ups(target_dk, action["set_ups"])
                        log.info(
                            "Rule '%s': set UPS=%s on %s",
                            rule.get("name"),
                            action["set_ups"],
                            target_dk,
                        )
                    else:
                        log.warning(
                            "Rule '%s': device %s does not have UPS control, skipping action",
                            rule.get("name"),
                            target_dk,
                        )

                if "set_state" in action:
                    self._set_rule_state(action["set_state"])
                    log.info("Rule '%s': set state=%s", rule.get("name"), action["set_state"])

                if "run_command" in action:
                    self._run_rule_command(
                        action["run_command"],
                        rule=rule,
                        action=action,
                        device_key=device_key,
                        target_device_key=target_dk,
                        kv=kv,
                        battery_pct=battery_pct,
                    )

            except Exception as e:
                log.error("Rule evaluation error: %s", e)

    def _run_init_rules(self) -> None:
        """Evaluate rules with condition.init once after devices are available."""
        if self._init_rules_ran:
            return
        self._init_rules_ran = True
        for device in self.devices:
            device_key = device["device_key"]
            kv = self.latest_data.get(device_key, {})
            battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
            self._evaluate_rules(device_key, kv, battery_pct, init=True)

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

                            # Only mark as local data if it contains telemetry fields
                            has_telemetry = self._has_telemetry_fields(kv)
                            if has_telemetry:
                                self._local_data_keys.add(dk)  # Mark as local data
                                log.debug("BLE data contains telemetry for %s", dk)
                            else:
                                log.debug(
                                    "BLE data is settings-only for %s (telemetry from cloud)", dk
                                )

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
                        log.debug(
                            "Local TCP connection failed for %s (pinned IP, failure #%d) — skipping to cloud",
                            dk,
                            failure_count,
                        )
                    elif failure_count >= 5:
                        # Auto-discovered device with 5+ failures: try re-discovery
                        log.debug(
                            "Local TCP connection failed for %s (%d consecutive failures), attempting re-discovery...",
                            dk,
                            failure_count,
                        )
                        if self._rediscover_device(dk):
                            # Re-discovered at new IP, try connecting again
                            lt = self.local_transports.get(dk)  # Get updated transport
                            if lt:
                                connected = self._connect_local(dk)
                    else:
                        # Auto-discovered device with <5 failures: skip to cloud
                        log.debug(
                            "Local TCP connection failed for %s (auto-discovered, failure #%d) — skipping to cloud",
                            dk,
                            failure_count,
                        )

                if lt.connected:
                    try:
                        kv = lt.read_status()
                        if kv:
                            log.debug("Got status via LOCAL TCP for %s", dk)
                            self._merge_device_data(dk, kv)

                            # Only mark as local data if it contains telemetry fields
                            # E3600/E3800 local TCP returns ONLY settings (14 fields), no telemetry
                            # We need to let MQTT cloud data be the primary source for these devices
                            has_telemetry = self._has_telemetry_fields(kv)
                            if has_telemetry:
                                self._local_data_keys.add(dk)  # Mark as local data
                                log.debug("Local TCP data contains telemetry for %s", dk)
                            else:
                                log.debug(
                                    "Local TCP data is settings-only for %s (telemetry from cloud)",
                                    dk,
                                )

                            self._process_data(dk, kv, source="LOCAL TCP")
                            # Reset failure counter on successful read
                            self._local_connect_failures[dk] = 0
                            # DON'T continue — still need to publish MQTT read request
                            # E3600/E3800 local TCP only returns settings, need cloud for telemetry
                    except Exception as e:
                        log.warning("Local TCP read failed for %s: %s", dk, e)

            # Always publish MQTT read request (even if local TCP connected)
            # E3600/E3800 local TCP only returns settings — we NEED cloud MQTT for telemetry
            if self.mqtt_client:
                cid = self._channel_id(device)
                pkt = build_ttlv_read(self._next_packet_id())
                topic = f"q/1/d/{cid}/bus"
                result = self.mqtt_client.publish(topic, pkt, qos=1)
                log.debug("Published TTLV read to %s (rc=%s, mid=%s)", topic, result.rc, result.mid)

            # If we haven't received MQTT data for this device yet, try REST API.
            # In rest_only mode there is no MQTT or local TCP, so we must re-fetch
            # every poll rather than only on the first time (fix from @brucehoult
            # in issue #14; otherwise --rest-only stops updating after cycle 1).
            if self.rest_only or dk not in self.latest_data:
                if self.token_data:  # Only available if not in offline mode
                    log.debug(
                        "Fetching status via REST API for %s (rest_only=%s, cached=%s)...",
                        dk,
                        self.rest_only,
                        dk in self.latest_data,
                    )
                    kv = get_device_properties_rest(
                        self.token_data["token"], self.region, device["product_key"], dk
                    )
                    if kv:
                        log.info("Got status via REST API for %s", dk)
                        self._merge_device_data(dk, kv)
                        self._process_data(dk, kv, source="REST API")

    # --- Restore outputs after shutdown (issue #59) ---

    @staticmethod
    def _coerce_switch(v):
        """ac_switch_hm / dc_switch_hm land in latest_data as bool, "ON"/"OFF" string,
        0/1 int, or absent. Coerce to bool for snapshot/restore comparisons. None
        passes through to signal "unobserved." """
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            return v.upper() in ("ON", "TRUE", "1")
        return bool(v)

    def _extract_soc(self, kv: dict):
        """Pull SoC % from kv with the same precedence as SENSOR_FIELDS["battery_percent"]:
        host_packet_data_jdb.host_packet_electric_percentage first, then top-level
        battery_percentage. Returns None if neither is present."""
        host = kv.get("host_packet_data_jdb")
        if isinstance(host, dict):
            v = host.get("host_packet_electric_percentage")
            if v is not None:
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    pass
        v = kv.get("battery_percentage")
        if v is not None:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
        return None

    def _extract_voltage(self, kv: dict):
        """Pull battery voltage from kv. Returns None for missing/invalid values."""
        for value in (kv.get("voltage"), _get_kv(kv, SENSOR_FIELDS["voltage"], 0)):
            try:
                voltage = float(value)
            except (TypeError, ValueError):
                continue
            if voltage > 0:
                return voltage
        return None

    def _extract_power(self, kv: dict, total_key: str, ac_key: str, dc_key: str):
        """Resolve a power channel (W) for rule conditions.

        Returns the top-level total when present and non-zero, else the AC+DC
        component sum when BOTH components are present (mirrors the fallback in
        _process_data so rules see the same value as status logging), else a
        genuinely-reported 0, else None when the value is unknown. Returning None
        lets `_below`/`_above` conditions decline to fire on missing telemetry
        instead of treating absent load as 0 W (unsafe for charge automation)."""

        def _as_int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        total = _as_int(_get_kv(kv, SENSOR_FIELDS[total_key]))
        ac = _get_kv(kv, SENSOR_FIELDS[ac_key])
        dc = _get_kv(kv, SENSOR_FIELDS[dc_key])
        components = None
        if ac is not None and dc is not None:
            a, d = _as_int(ac), _as_int(dc)
            if a is not None and d is not None:
                components = a + d
        if total is not None and total != 0:
            return total
        if components is not None:
            return components
        return total  # genuine 0, or None when unknown

    def _restore_cfg(self) -> dict:
        return self.config.get("restore_outputs_after_shutdown", {}) or {}

    def _on_device_offline(self, device_key: str):
        """Called when a `is now offline` event arrives.

        Snapshot the user's current AC/DC switch state to disk if telemetry
        indicates a low-battery shutdown. Percentage remains the default
        threshold; voltage can be configured for LFP packs whose SoC estimate
        drifts near empty.
        """
        now = time.time()
        self._last_offline_at[device_key] = now
        log.info("Device %s is now offline", device_key)

        cfg = self._restore_cfg()
        if not cfg.get("enabled", False):
            return

        kv = self.latest_data.get(device_key, {})
        soc = self._extract_soc(kv)
        voltage = self._extract_voltage(kv)
        threshold_pct = int(cfg.get("shutdown_threshold_pct", 10))
        threshold_voltage = cfg.get("shutdown_threshold_voltage")
        if threshold_voltage is not None:
            threshold_voltage = float(threshold_voltage)

        pct_crossed = soc is not None and soc <= threshold_pct
        voltage_crossed = (
            threshold_voltage is not None and voltage is not None and voltage <= threshold_voltage
        )
        if not pct_crossed and not voltage_crossed:
            if soc is None and (threshold_voltage is None or voltage is None):
                log.debug(
                    "No usable SoC/voltage observation for %s at offline transition; "
                    "skipping snapshot",
                    device_key,
                )
            else:
                log.debug(
                    "Device %s went offline at SoC=%s%% voltage=%sV; thresholds "
                    "are SoC<=%d%% voltage<=%sV; skipping snapshot.",
                    device_key,
                    soc if soc is not None else "unknown",
                    f"{voltage:.1f}" if voltage is not None else "unknown",
                    threshold_pct,
                    threshold_voltage if threshold_voltage is not None else "disabled",
                )
            return

        ac_on = self._coerce_switch(kv.get("ac_switch_hm"))
        dc_on = self._coerce_switch(kv.get("dc_switch_hm"))
        # If either switch state is unobservable, snapshot what we have (default
        # missing to False rather than refuse to snapshot — restore worker will
        # only act on differences from observed live state anyway).
        snap = output_state.OutputSnapshot.now(
            ac_on=bool(ac_on) if ac_on is not None else False,
            dc_on=bool(dc_on) if dc_on is not None else False,
            soc_at_offline=soc,
            voltage_at_offline=voltage,
        )
        try:
            output_state.save(device_key, snap)
        except OSError as e:
            log.warning("Could not persist output snapshot for %s: %s", device_key, e)
            return
        log.info(
            "Snapshotted output state for %s for shutdown-restore "
            "(SoC=%s%%, voltage=%sV, AC=%s, DC=%s)",
            device_key,
            soc if soc is not None else "unknown",
            f"{voltage:.1f}" if voltage is not None else "unknown",
            ac_on,
            dc_on,
        )

    def _on_device_online(self, device_key: str):
        """Called when a `is now online` event arrives. If a snapshot exists
        and the offline gap was long enough to be a real shutdown, spawns a
        background worker to restore the AC/DC state."""
        now = time.time()
        self._last_online_at[device_key] = now
        log.info("Device %s is now online", device_key)

        cfg = self._restore_cfg()
        if not cfg.get("enabled", False):
            return

        snap = output_state.get(device_key)
        if snap is None:
            return  # No snapshot, no restore.

        max_age = int(cfg.get("snapshot_max_age_seconds", 86400))
        age = snap.age_seconds()
        if age > max_age:
            log.warning(
                "Discarding stale output snapshot for %s (age %.0fs > max %ds)",
                device_key,
                age,
                max_age,
            )
            output_state.clear(device_key)
            return

        min_offline = int(cfg.get("minimum_offline_seconds", 120))
        last_offline = self._last_offline_at.get(device_key)
        if last_offline is not None and (now - last_offline) < min_offline:
            log.info(
                "Online transition for %s within %.1fs of last offline (<%d "
                "minimum) — too brief to be a real shutdown, skipping restore.",
                device_key,
                now - last_offline,
                min_offline,
            )
            return

        existing = self._restore_threads.get(device_key)
        if existing is not None and existing.is_alive():
            log.debug("Restore worker already running for %s; skipping duplicate spawn", device_key)
            return

        log.info(
            "Starting restore worker for %s — target AC=%s, DC=%s "
            "(snapshot taken %.0fs ago at SoC=%d%%)",
            device_key,
            snap.ac_on,
            snap.dc_on,
            age,
            snap.soc_at_offline,
        )
        t = threading.Thread(
            target=self._restore_outputs_worker,
            args=(device_key, bool(snap.ac_on), bool(snap.dc_on)),
            daemon=True,
            name=f"restore-{device_key[:6]}",
        )
        self._restore_threads[device_key] = t
        t.start()

    def _restore_outputs_worker(self, device_key: str, target_ac: bool, target_dc: bool):
        """Retry loop: every `retry_interval_seconds` (default 30s), check if
        observed AC/DC state matches the target and re-issue commands if not.
        Bail out when both match (success), when timeout elapses, or when the
        monitor is shutting down. State verification is via observed
        latest_data — robust against the LCD-at-0%-silently-rejects pattern
        Bruce documented in #57."""
        cfg = self._restore_cfg()
        interval = max(1, int(cfg.get("retry_interval_seconds", 30)))
        timeout = int(cfg.get("retry_timeout_seconds", 600))
        started_at = time.time()

        while time.time() - started_at < timeout:
            if not self._running:
                log.info("Monitor shutting down; restore worker for %s exiting", device_key)
                return

            kv = self.latest_data.get(device_key, {}) or {}
            current_ac = self._coerce_switch(kv.get("ac_switch_hm"))
            current_dc = self._coerce_switch(kv.get("dc_switch_hm"))

            if current_ac == target_ac and current_dc == target_dc:
                log.info("Restore complete for %s: AC=%s DC=%s", device_key, target_ac, target_dc)
                output_state.clear(device_key)
                return

            if current_ac != target_ac:
                log.info(
                    "Restore: setting AC=%s on %s (currently %s)", target_ac, device_key, current_ac
                )
                try:
                    self.set_ac(device_key, target_ac)
                except Exception as e:
                    log.warning("Restore: set_ac failed for %s: %s", device_key, e)

            if current_dc != target_dc:
                log.info(
                    "Restore: setting DC=%s on %s (currently %s)", target_dc, device_key, current_dc
                )
                try:
                    self.set_dc(device_key, target_dc)
                except Exception as e:
                    log.warning("Restore: set_dc failed for %s: %s", device_key, e)

            time.sleep(interval)

        kv = self.latest_data.get(device_key, {}) or {}
        log.error(
            "Restore for %s timed out after %ds; clearing snapshot. "
            "Target was AC=%s DC=%s; current is AC=%s DC=%s",
            device_key,
            timeout,
            target_ac,
            target_dc,
            self._coerce_switch(kv.get("ac_switch_hm")),
            self._coerce_switch(kv.get("dc_switch_hm")),
        )
        output_state.clear(device_key)

    def _token_needs_refresh(self) -> bool:
        if self.offline_mode:
            return False
        if not self.token_data:
            return True
        return time.time() > (self.token_data["expires_at"] - 300)

    def _try_cloud_recovery(self) -> bool:
        """Retry cloud login after a prior transient failure (issue #23).

        Only triggers when we're in offline_mode AND the fallback was unplanned
        (not --offline). On success, rejoins cloud MQTT and clears the flag.
        """
        if not self.offline_mode or not self._fell_back_to_offline:
            return False
        interval = self.config.get("cloud_retry_interval", 300)
        now = time.time()
        if now - self._last_cloud_retry_at < interval:
            return False
        self._last_cloud_retry_at = now

        # Phase 1: prove cloud is reachable before mutating any state.
        try:
            log.info("Retrying cloud login (offline recovery)...")
            token = login(self.config["email"], self.config["password"], self.region)
            devices = resolve_devices(self.config, token["token"], self.region)
            if not devices:
                raise RuntimeError("Cloud login succeeded but no devices resolved")
        except Exception as e:
            log.info("Cloud retry failed: %s (next attempt in %ds)", e, interval)
            return False

        # Phase 2: login succeeded. Apply state and rebuild MQTT/local transports.
        self.token_data = token
        self.devices = devices
        self.offline_mode = False
        self._fell_back_to_offline = False
        log.info("Cloud recovered, reconnecting MQTT")
        try:
            if not self.skip_local_setup:
                self._setup_local_transports()
            self.connect_mqtt()
        except Exception as e:
            # A post-login MQTT/local failure shouldn't wedge us back in offline mode,
            # but log loudly so operators see it. paho's auto-reconnect + the HA retry
            # loop will recover on their own.
            log.warning("Cloud recovered but MQTT/local setup hit an error: %s", e)
        return True

    def _recover_mqtt_connection(self) -> bool:
        """Rebuild the MQTT client after broker CONNACK failures."""
        if self.offline_mode or getattr(self, "rest_only", False):
            return False
        if self._mqtt_connect_failures == 0:
            return False

        interval = self.config.get("mqtt_reconnect_interval", 60)
        now = time.time()
        if now - self._last_mqtt_rebuild_at < interval:
            return False
        self._last_mqtt_rebuild_at = now

        failures = self._mqtt_connect_failures
        log.warning("Rebuilding MQTT client after %d connection failure(s)", failures)
        try:
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
        except Exception as e:
            log.debug("Ignoring MQTT disconnect error during rebuild: %s", e)

        self._mqtt_connect_failures = 0
        try:
            self.connect_mqtt()
        except Exception as e:
            self._mqtt_connect_failures = failures
            log.warning("MQTT rebuild failed: %s", e)
            return False
        return True

    # --- MQTT connection ---

    def connect_mqtt(self):
        if self.offline_mode:
            log.info("Offline mode — skipping MQTT connection")
            return

        if getattr(self, "rest_only", False):
            log.info("REST-only mode — skipping MQTT connection")
            return

        client_id = f"qu_{self.token_data['uid']}_{int(time.time() * 1000)}"
        self.mqtt_client = mqtt.Client(
            client_id=client_id,
            transport="websockets",
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self.mqtt_client.ws_set_options(path=self.region["mqtt_path"])
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set(username="", password=self.token_data["token"])
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_message = self._on_message
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
        log.info(
            "Connecting to MQTT broker %s:%d...", self.region["mqtt_host"], self.region["mqtt_port"]
        )
        self.mqtt_client.connect(self.region["mqtt_host"], self.region["mqtt_port"])
        self.mqtt_client.loop_start()

    # --- Main loop ---

    def run(self, enable_ha=False, force_offline=False):
        self._running = True

        # Fail fast on a broken cloud poll_interval before any cloud login or
        # MQTT connection. Local/offline mode is not subject to the cloud quota.
        poll_interval = self.config.get("poll_interval", RECOMMENDED_POLL_INTERVAL)
        _validate_poll_interval_for_mode(poll_interval, force_offline)

        self.authenticate(force_offline=force_offline)
        self.connect_mqtt()

        if enable_ha:
            from ha_bridge import HomeAssistantBridge

            ha_config = self.config.get("homeassistant", {})
            if ha_config.get("enabled") or enable_ha:
                self.ha_bridge = HomeAssistantBridge(ha_config, self.devices)
                self.ha_bridge.command_callback = self._ha_command
                self.ha_bridge.connect()

        self._run_init_rules()

        log.info("Monitoring started (polling every %ds)", poll_interval)

        # Smart high-freq warm-up mode:
        # Enable high-frequency MQTT reporting for a short warm-up period to quickly
        # populate initial data (E3600/E3800 send telemetry in 3 alternating packet shapes).
        # After warm-up, disable high-freq to avoid burning cloud quota (error code 4026).
        # Only useful when cloud MQTT is connected — skip in offline/local-only mode.
        high_freq_warmup_seconds = self.config.get("high_freq_warmup_seconds", 60)
        if self.mqtt_client and high_freq_warmup_seconds > 0:
            log.info(
                "Enabling high-freq reporting for %ds warm-up period...", high_freq_warmup_seconds
            )
            self._enable_high_freq_reporting()
            warmup_start = time.time()

        time.sleep(3)
        self._request_status()

        try:
            while self._running:
                time.sleep(poll_interval)

                # Check if warm-up period has ended — disable high-freq to save cloud quota
                if self.mqtt_client and high_freq_warmup_seconds > 0:
                    elapsed = time.time() - warmup_start
                    if elapsed >= high_freq_warmup_seconds:
                        log.info(
                            "Warm-up complete (%.0fs) — disabling high-freq to preserve cloud quota",
                            elapsed,
                        )
                        self._disable_high_freq_reporting()
                        high_freq_warmup_seconds = 0  # Prevent re-disabling on every loop

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
                else:
                    # Issue #23: if we previously fell back to offline due to a
                    # transient cloud failure, periodically attempt to recover.
                    self._try_cloud_recovery()
                    self._recover_mqtt_connection()

                # Issue #23 (secondary): retry local HA MQTT if it failed to
                # connect at startup or was lost.
                if self.ha_bridge:
                    self.ha_bridge.try_reconnect()

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

    @staticmethod
    def _high_freq_effective(device: dict) -> bool:
        """Return False for models where high_frequency_reporting is a known no-op
        (issue #14: E3600LFP ignores the setting; don't waste cloud requests)."""
        name = device.get("device_name") or device.get("product_name") or ""
        return MODEL_BEHAVIOR.get(name, {}).get("high_freq_effective", True)

    def _enable_high_freq_reporting(self, stagger: float = 0):
        """Enable high-frequency MQTT reporting on all devices for fast cache warm-up.

        Args:
            stagger: Seconds to wait between devices (helps cloud process multi-device requests)
        """
        effective = [d for d in self.devices if self._high_freq_effective(d)]
        skipped = [d for d in self.devices if not self._high_freq_effective(d)]
        for d in skipped:
            log.debug(
                "Skipping high-freq enable for %s (ineffective on this model, issue #14)",
                d.get("device_name") or d["device_key"],
            )
        for i, d in enumerate(effective):
            dk = d["device_key"]
            try:
                # high_frequency_reporting is transient: device auto-reverts the
                # value so a post-write read-back will mismatch and log a noisy
                # but meaningless warning (issue #50). Skip verification here.
                self.send_control(dk, "high_frequency_reporting", 3, verify=False)
                log.info("Enabled high-freq reporting for %s", dk)
            except Exception as e:
                log.debug("Could not enable high-freq for %s: %s", dk, e)
            if stagger > 0 and i < len(effective) - 1:
                time.sleep(stagger)

    def _disable_high_freq_reporting(self):
        """Disable high-frequency reporting after warm-up period."""
        for d in self.devices:
            if not self._high_freq_effective(d):
                continue  # never enabled → nothing to disable
            dk = d["device_key"]
            try:
                # Transient control code; suppress read-back verification (#50).
                self.send_control(dk, "high_frequency_reporting", 0, verify=False)
                log.info("Disabled high-freq reporting for %s (warm-up complete)", dk)
            except Exception as e:
                log.debug("Could not disable high-freq for %s: %s", dk, e)

    def _ha_command(self, device_key: str, control: str, on: bool):
        """Handle commands from Home Assistant.

        The slug→TSL map below must mirror every switch published by
        ha_bridge._publish_discovery with a command_topic. If discovery adds
        a new switch, add the slug→TSL row here too. Issue #54: previously
        only ac/dc/ups were mapped, so HA toggles for eco_mode, touch_lock,
        and auto_dim (auto_light_flag_as) were silently dropped. The longer
        term cleanup is to drive this map from discovery; not in this PR.
        """
        ctrl_map = {
            "ac": "ac_switch_hm",
            "dc": "dc_switch_hm",
            "ups": "ups_status_hm",
            "eco_mode": "eco_quite_mode_as",
            "touch_lock": "device_touch_locking_as",
            # auto_dim's command_topic uses the TSL field name directly
            # (see ha_bridge.py:628), so the slug == the TSL code here.
            "auto_light_flag_as": "auto_light_flag_as",
        }
        code = ctrl_map.get(control)
        if code:
            self.send_bool_control(device_key, code, on)
        else:
            log.warning(
                "HA command for unknown control %r on %s -- no slug->TSL mapping (issue #54)",
                control,
                device_key,
            )

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

    def status_once(self, force_offline: bool = False, skip_local: Optional[bool] = None):
        self.authenticate(force_offline=force_offline, skip_local=skip_local)
        if not self.offline_mode:
            self.connect_mqtt()
            time.sleep(3)
            # Enable high-freq reporting for devices that need it (E3600/E3800)
            # Stagger requests to avoid cloud throttling for multi-device setups.
            # Use the shared helper so the per-model skip (issue #14) applies here too.
            if self.mqtt_client:
                self._enable_high_freq_reporting(stagger=1)
                time.sleep(2)  # Give devices time to switch modes
        self._request_status()

        # E3600/E3800 sends telemetry in alternating MQTT packets at ~10-15s intervals.
        # Use an active polling loop: check every 5s, re-request for incomplete devices,
        # give up after max_wait seconds total.
        max_wait = 45  # Total max wait (was 30s rigid, now smarter)
        check_interval = 5
        elapsed = 0
        all_device_keys = {d["device_key"] for d in self.devices}
        last_request_time = 0

        log.info(
            "Collecting data (up to %ds for multi-packet devices like E3600/E3800)...", max_wait
        )

        while elapsed < max_wait:
            time.sleep(check_interval)
            elapsed += check_interval

            # Check which devices still need telemetry
            incomplete = []
            for dk in all_device_keys:
                kv = self.latest_data.get(dk, {})
                if not self._has_telemetry_fields(kv):
                    incomplete.append(dk)

            if not incomplete:
                log.info("All devices have telemetry data (%ds)", elapsed)
                break

            # Re-request data for incomplete devices every 10s
            if elapsed - last_request_time >= 10:
                log.info(
                    "Still waiting for telemetry from: %s (%ds/%ds)...",
                    ", ".join(incomplete),
                    elapsed,
                    max_wait,
                )
                # Re-publish MQTT read requests for incomplete devices
                if self.mqtt_client:
                    for dk in incomplete:
                        device = self._find_device(dk)
                        if device:
                            cid = self._channel_id(device)
                            pkt = build_ttlv_read(self._next_packet_id())
                            self.mqtt_client.publish(f"q/1/d/{cid}/bus", pkt, qos=1)
                last_request_time = elapsed

        if elapsed >= max_wait:
            incomplete = [
                dk
                for dk in all_device_keys
                if not self._has_telemetry_fields(self.latest_data.get(dk, {}))
            ]
            if incomplete:
                log.warning("Timed out waiting for telemetry from: %s", ", ".join(incomplete))

        def _norm_model_key(value: str) -> str:
            return "".join(ch for ch in str(value).upper() if ch.isalnum())

        capacity_lookup = {
            _norm_model_key(model): float(capacity)
            for model, capacity in BATTERY_CAPACITY_WH.items()
        }

        def _fmt_hours(hours: float) -> str:
            if hours < 0:
                return "N/A"
            total_min = int(hours * 60)
            days, rem = divmod(total_min, 24 * 60)
            h, m = divmod(rem, 60)
            if days > 0:
                return f"{days}d {h}h {m}m"
            return f"{h}h {m}m"

        for dk, kv in self.latest_data.items():
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

            # Compute net power drain on host battery
            net_drain = float(_get_kv(kv, SENSOR_FIELDS["voltage"], 0)) * float(
                _get_kv(kv, SENSOR_FIELDS["current"], 0)
            )
            net_drain_label = "Net Drain: " if net_drain < 0 else "Net Charge:"

            remain = int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0))
            # Check if remain_time is unreliable (local transports often return bogus values)
            battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
            if source in ("LOCAL TCP", "BLE") and remain <= 5 and battery_pct > 50:
                remain_str = "N/A (unreliable from local)"
            else:
                remain_str = f"{remain // 60}h {remain % 60}m" if remain > 0 else "N/A"

            charge_remain = int(_get_kv(kv, SENSOR_FIELDS["remain_charging_time"], 0))
            charge_remain_str = (
                f"{charge_remain // 60}h {charge_remain % 60}m" if charge_remain > 0 else "N/A"
            )

            # Compute estimated time-to-empty/full from capacity table + battery % + net battery power.
            model_info = self._find_device(dk)
            model_candidates = [
                model_info.get("product_name", ""),
                model_info.get("device_name", ""),
            ]
            capacity_wh = None
            for candidate in model_candidates:
                key = _norm_model_key(candidate)
                if key in capacity_lookup:
                    capacity_wh = capacity_lookup[key]
                    break

            est_time_str = "N/A"
            if capacity_wh and 0 <= battery_pct <= 100:
                current_charge_wh = capacity_wh * (battery_pct / 100.0)
                net_power_w = net_drain  # <0 discharge, >0 charge
                if net_power_w != 0:
                    if net_power_w < 0:
                        est_time_str = _fmt_hours(current_charge_wh / abs(net_power_w))
                    else:
                        est_time_str = _fmt_hours((capacity_wh - current_charge_wh) / net_power_w)

            status_value = int(_get_kv(kv, SENSOR_FIELDS["device_status_hm"], -1))
            status_str = (
                DEVICE_STATUS_LABELS.get(int(status_value), str(status_value))
                if status_value >= 0
                else "Unknown"
            )

            print(f"\n{'=' * 50}")
            print(f"Device: {dk}")
            print(f"Connection: {source}")
            print(f"{'=' * 50}")
            print(f"Status:        {status_str} ({status_value})")
            print(f"Battery:       {_get_kv(kv, SENSOR_FIELDS['battery_percent'], '?')}%")
            print(f"Voltage:       {float(_get_kv(kv, SENSOR_FIELDS['voltage'], 0)):.1f}V")
            print(f"Temperature:   {_get_kv(kv, SENSOR_FIELDS['temperature'], '?')}°C")
            print(f"Discharge time:{remain_str}")
            print(f"Charge time:   {charge_remain_str}")
            print(f"{net_drain_label}    {abs(net_drain):.1f}W")
            if capacity_wh:
                print(f"Capacity:      {capacity_wh:.0f}Wh")
                if net_drain < 0:
                    print(f"Est. Empty:    {est_time_str}")
                elif net_drain > 0:
                    print(f"Est. Full:     {est_time_str}")
            else:
                print("Capacity:      Unknown (not in BATTERY_CAPACITY_WH)")
            print(f"Total Input:   {total_in}W")
            print(f"Total Output:  {total_out}W")
            print(
                f"AC Output:     {_get_kv(kv, SENSOR_FIELDS['ac_output_power'], 0)}W @ {_get_kv(kv, SENSOR_FIELDS['ac_output_voltage'], '?')}V"
            )
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
                        pack_battery = int(float(pack.get("charging_pack_battery", 0)))
                    except (ValueError, TypeError):
                        pack_battery = 0
                    if pack_battery == 0 and 5 <= pack_status_val <= 100:
                        pack_battery = pack_status_val
                        log.debug(
                            "Using charging_pack_status (%d%%) as battery for pack %d",
                            pack_status_val,
                            i,
                        )
                    print(
                        f"Pack {i}:        {pack_battery if pack_battery > 0 else '?'}% "
                        f"{float(pack.get('charging_pack_voltage', 0)):.1f}V"
                    )

        if not self.latest_data:
            print("No data received — device may be offline.")

        # Leave the device in its normal cadence. Don't strand it in high-freq
        # mode after a one-shot --status run; that would burn cloud quota if
        # another process checks status often. Cheap no-op for skipped models.
        if self.mqtt_client:
            self._disable_high_freq_reporting()
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

    def stop(self):
        self._running = False
