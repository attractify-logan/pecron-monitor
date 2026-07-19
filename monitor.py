"""
Main monitoring logic for pecron-monitor.

Contains the PecronMonitor class which orchestrates cloud authentication,
MQTT connection, local transport management, and data processing.
"""

import logging
import threading


from helpers import _get_kv, _get_kv_single
from constants import REGIONS, SENSOR_FIELDS
from monitor_alerts import MonitorAlertsMixin
from monitor_cloud import MonitorCloudMixin
from monitor_controls import MonitorControlsMixin
from monitor_polling import MonitorPollingMixin
from monitor_restore import MonitorRestoreMixin
from monitor_rules import MonitorRulesMixin
from monitor_status import MonitorStatusMixin


log = logging.getLogger("pecron")


class PecronMonitor(
    MonitorCloudMixin,
    MonitorControlsMixin,
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
