"""
Main monitoring logic for pecron-monitor.

Contains the PecronMonitor class which orchestrates cloud authentication,
MQTT connection, local transport management, and data processing.
"""

import json
import logging
import threading
import time


try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from helpers import _get_kv, _get_kv_single
from typing import Any, Optional
from constants import REGIONS, DEFAULT_CONTROLS, SENSOR_FIELDS
from cloud_api import login, resolve_devices, set_device_property_rest
from protocol import build_ttlv_write_bool, build_ttlv_write_enum
from monitor_alerts import MonitorAlertsMixin
from monitor_polling import MonitorPollingMixin
from monitor_restore import MonitorRestoreMixin
from monitor_rules import MonitorRulesMixin
from monitor_status import MonitorStatusMixin


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


class PecronMonitor(
    MonitorPollingMixin,
    MonitorStatusMixin,
    MonitorAlertsMixin,
    MonitorRulesMixin,
    MonitorRestoreMixin,
):
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
        self.rule_states = self._load_rule_states()
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

        # Only attempt local radio writes for boolean switches (hardware limitation)
        if ctrl_type == "BOOL":
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

        # Fall back to cloud transports
        # Fix: Route non-boolean configurations (ENUM/INT) straight to REST API (issue #84)
        if ctrl_type != "BOOL" and self.token_data:
            if set_device_property_rest(
                self.token_data["token"],
                self.region,
                device["product_key"],
                device_key,
                {control_code: value},
            ):
                log.info("Sent %s=%s to %s via CLOUD REST API", control_code, value, device_key)
                return True

        # Boolean primary cloud transport / last-resort fallback channel
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

        # Last-resort cloud backup for Boolean toggles if MQTT client instance drops
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
        cycle_elapsed = self._request_status_with_local_retries(poll_interval)

        try:
            while self._running:
                if poll_interval > 0:
                    cycle_remainder = cycle_elapsed % poll_interval
                    cycle_delay = (
                        poll_interval if cycle_remainder == 0 else poll_interval - cycle_remainder
                    )
                else:
                    cycle_delay = 0.0
                time.sleep(cycle_delay)
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

                cycle_elapsed = self._request_status_with_local_retries(poll_interval)

        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            self._running = False
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            if self.ha_bridge:
                self.ha_bridge.disconnect()

    def _ha_command(self, device_key: str, control: str, payload: Any):
        """Handle commands from Home Assistant."""
        ctrl_map = {
            "ac": "ac_switch_hm",
            "dc": "dc_switch_hm",
            "ups": "ups_status_hm",
            "eco_mode": "eco_quite_mode_as",
            "touch_lock": "device_touch_locking_as",
            "auto_light_flag_as": "auto_light_flag_as",
        }
        code = ctrl_map.get(control)
        if code:
            if isinstance(payload, bool):
                is_on = payload
            else:
                is_on = str(payload).upper() in ("ON", "TRUE", "1")
            self.send_bool_control(device_key, code, is_on)
        elif control == "ac_charging_power":
            val_str = str(payload).strip()
            is_percent = "%" in val_str
            val_str = val_str.replace("%", "")
            try:
                val = int(val_str)
                if is_percent:
                    index = val // 10
                else:
                    index = val // 10 if val > 10 else val

                if 0 <= index <= 10:
                    self.send_control(device_key, "ac_charging_power_ios", index)
            except (ValueError, TypeError):
                log.warning("Invalid AC charging power payload from HA: %r", payload)
        elif control == "ups_charge_threshold":
            val_str = str(payload).replace("%", "").strip()
            try:
                pct = int(val_str)
                if 30 <= pct <= 100:
                    self.send_control(device_key, "ups_start_charge_value_as", pct)
            except (ValueError, TypeError):
                log.warning("Invalid UPS charge threshold payload from HA: %r", payload)
        else:
            log.warning(
                "HA command for unknown control %r on %s -- no slug->TSL mapping (issue #54)",
                control,
                device_key,
            )

    def stop(self):
        self._running = False
