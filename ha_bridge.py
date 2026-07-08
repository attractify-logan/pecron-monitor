"""
Home Assistant MQTT bridge for pecron-monitor.

Publishes Home Assistant MQTT auto-discovery config and state updates.
"""

import json
import logging
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from helpers import _truthy, _get_kv_single, _fmt_dhm
from constants import (
    SENSOR_FIELDS,
    DEVICE_STATUS_LABELS,
    FAULT_ALARM_LABELS,
    WB_CHARGE_VOLTAGE_LABELS,
    WB_DISCHARGE_VOLTAGE_LABELS,
    WB_CHARGE_CURRENT_LABELS,
    WB_DISCHARGE_CURRENT_LABELS,
    WB_HEATING_MODE_LABELS,
    PACK_STATUS_LABELS,
)

log = logging.getLogger("pecron")


# Home Assistant device view categorization for MQTT discovery (issue #34).
# Entities in this map get an `entity_category` hint on discovery so HA
# groups them under collapsible Configuration / Diagnostic sections instead
# of flooding the main device view. Keys omitted here default to the main
# view. Pack-level sensors (pack_N_*) are routed to diagnostic via a prefix
# rule in entity_category_for() below so we don't need 20 lines of per-pack
# entries here.
ENTITY_CATEGORIES = {
    # Configuration: knobs set rarely, not part of the daily glance.
    "ups": "config",
    "eco_mode": "config",
    "touch_lock": "config",
    "bypass": "config",
    "auto_dim": "config",
    "beep": "config",
    "ac_charging_power": "config",
    "ups_charge_threshold": "config",
    "standby_timeout": "config",
    "screen_brightness": "config",
    "ac_voltage_setting": "config",
    "ac_frequency_setting": "config",
    "ac_output_voltage": "config",
    "ac_output_hz": "config",
    "auto_off_timer": "config",
    "charging_limit_voltage": "config",
    "discharge_limit_voltage": "config",
    "charging_current_limit": "config",
    "discharge_current_limit": "config",
    "battery_heating": "config",
    # Diagnostic: detail, only looked at when debugging.
    "host_battery": "diagnostic",
    "battery_temp": "diagnostic",
    "charging_plate_temp": "diagnostic",
    "inverter_temp": "diagnostic",
    "ac_input": "diagnostic",
    "dc_input": "diagnostic",
    "ac_output_pf": "diagnostic",
    # DC5521 barrel jack is typically the AC adapter brick input, not a user-
    # interesting reading for most setups. Keep it in diagnostic.
    "dc5521_input_voltage": "diagnostic",
    "dc5521_input_current": "diagnostic",
    "dc5521_input_power": "diagnostic",
    # GX16-MF1 / GX16-MF2 are the solar inputs. On van / RV / off-grid setups
    # these are the primary story, so they stay in the main device view. Idle
    # ports still get suppressed to null by the port-gating logic in
    # publish_state, so unused solar inputs show Unknown rather than cluttering.
    "remaining_charging_time": "diagnostic",  # duplicates remaining_time due to Pecron API bug (jsight issue #1)
    "device_status": "diagnostic",
    "expansion_pack": "diagnostic",
    "fault_alarm": "diagnostic",
}

# Prefix match for per-pack sensors (pack_0_battery, pack_1_voltage, etc.)
_DIAGNOSTIC_PREFIXES = ("pack_",)


# Issue #78: sensor device_classes whose readings are instantaneous point-in-time
# measurements. HA only records long-term statistics for numeric sensors that
# declare a state_class, so without this these sensors are display-only — they
# never appear in history statistics and can't feed the Energy Dashboard via a
# Riemann-sum integral helper (power W -> energy kWh). The `energy` device_class
# is intentionally excluded: cumulative counters set state_class=total_increasing
# at their call site instead.
_MEASUREMENT_DEVICE_CLASSES = frozenset({"power", "voltage", "current", "temperature", "battery"})

# Issue #78 review: sensor keys that carry a numeric device_class but whose state
# publish_state decodes to a STRING label, not a number. These are the WB12200
# charge/discharge limits -> "12.8V" / "60A" via WB_*_LABELS. A numeric
# state_class on a string state is invalid in HA, so they're excluded from the
# injection below. Genuine numeric config sensors (e.g. ac_output_voltage) are
# NOT in this set and still get state_class. Keep in sync with the label decoder
# (_wb_enum_fields) in publish_state.
_LABEL_SENSOR_KEYS = frozenset(
    {
        "charging_limit_voltage",
        "discharge_limit_voltage",
        "charging_current_limit",
        "discharge_current_limit",
    }
)


def entity_category_for(key: str):
    """Return the HA entity_category ('config' / 'diagnostic') for an entity key,
    or None if the entity should stay in the main device view."""
    if key in ENTITY_CATEGORIES:
        return ENTITY_CATEGORIES[key]
    for prefix in _DIAGNOSTIC_PREFIXES:
        if key.startswith(prefix):
            return "diagnostic"
    return None


class HomeAssistantBridge:
    """Publishes Home Assistant MQTT auto-discovery config and state updates."""

    def __init__(self, ha_config: dict, devices: list):
        self.ha_config = ha_config
        self.devices = devices
        self.client = None
        self.discovery_prefix = ha_config.get("discovery_prefix", "homeassistant")
        self._connected = False

        # Retry state for the local MQTT broker (issue #23 follow-up).
        # If the broker is down at startup or drops later, try_reconnect() called
        # from the main loop will attempt a fresh connect every _retry_interval seconds.
        self._last_retry_at = 0.0
        self._retry_interval = ha_config.get("retry_interval", 60)
        # Clear current retained discovery payloads immediately before republishing
        # them. This forces Home Assistant to reload payload field changes on
        # service restart instead of requiring a manual MQTT integration reload.
        self.clear_discovery_on_startup = ha_config.get("clear_discovery_on_startup", True)
        self._clear_current_discovery = False

        # Deferred-discovery bookkeeping. Per-port DC-input entities
        # (dc5521, gx16mf1, gx16mf2) are only published the first time the
        # device reports any data for that port, so devices without the
        # hardware don't accumulate ghost Unknown entities in HA.
        # device_key -> dev_info dict (captured during _publish_discovery).
        self._device_dev_info: dict = {}
        # (device_key, port_name) set: ports we've already published discovery
        # for since this bridge connected.
        self._deferred_ports_published: set = set()

        # Cache last-known-good values per device so partial payloads don't zero-out entities
        self._state_cache = {}  # device_key -> dict of last published fields
        # Cache last-known values per device so partial payloads (host-only vs SOC-only)
        # don't clobber sensors to 0/unknown in Home Assistant.
        self._last_state = {}  # device_key -> dict

        # Issue #49: command topics captured during _publish_discovery so the
        # subscribe loop in on_connect stays in lockstep with what's actually
        # registered in HA. Adding a new switch with a command_topic to
        # discovery automatically wires its subscription -- no parallel
        # hardcoded list to drift against.
        self._command_topics: list = []

    def connect(self):
        """Initial connection attempt. If it fails the bridge is not fatal;
        the monitor's main loop will call try_reconnect() periodically."""
        self._last_retry_at = time.time()
        self._connect_attempt()

    def _connect_attempt(self):
        host = self.ha_config.get("mqtt_host", "localhost")
        port = self.ha_config.get("mqtt_port", 1883)
        user = self.ha_config.get("mqtt_user", "")
        pw = self.ha_config.get("mqtt_password", "")

        # Tear down any previous client before a fresh attempt.
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

        client = mqtt.Client(
            client_id="pecron_ha_bridge",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if user:
            client.username_pw_set(user, pw)

        def on_connect(client, ud, flags, rc, props=None):
            if rc == mqtt.CONNACK_ACCEPTED:
                self._connected = True
                log.info("Home Assistant MQTT bridge connected to %s:%d", host, port)
                self._publish_discovery()
                # Issue #49: subscribe to every command_topic that
                # _publish_discovery just registered. Single source of truth --
                # adding a new switch to discovery automatically wires its
                # subscription. Previously the subscribe loop hardcoded
                # ["ac", "dc", "ups"] and silently dropped commands sent to
                # eco_mode, touch_lock, auto_light_flag_as, etc.
                for topic in self._command_topics:
                    client.subscribe(topic, qos=1)

        def on_disconnect(client, ud, disconnect_flags, rc, props=None):
            # paho auto-reconnect handles this after a successful initial connect,
            # but we flip the flag so try_reconnect() is a no-op until paho gives up.
            if self._connected:
                log.warning("Home Assistant MQTT bridge disconnected (rc=%s)", rc)
            self._connected = False

        def on_message(client, ud, msg):
            # Handle HA commands
            parts = msg.topic.split("/")
            if len(parts) == 4 and parts[3] == "set":
                dk = parts[1]
                ctrl = parts[2]
                payload = msg.payload.decode().upper()
                self._handle_command(dk, ctrl, payload)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=self._retry_interval)
        try:
            client.connect(host, port)
            client.loop_start()
            self.client = client
        except (ConnectionRefusedError, OSError) as e:
            log.error(
                "Cannot connect to MQTT broker at %s:%d (%s). Will retry every %ds.",
                host,
                port,
                e,
                self._retry_interval,
            )
            self._connected = False
            self.client = None

    def try_reconnect(self) -> bool:
        """Retry the initial HA MQTT connection if it never succeeded.
        No-op once paho's auto-reconnect is handling an already-established session.
        Returns True when a retry attempt ran (regardless of outcome).
        """
        if self._connected:
            return False
        if self.client is not None:
            # paho is already trying in the background; don't fight it.
            return False
        now = time.time()
        if now - self._last_retry_at < self._retry_interval:
            return False
        self._last_retry_at = now
        log.info("Retrying Home Assistant MQTT connection...")
        self._connect_attempt()
        return True

    def _handle_command(self, device_key: str, control: str, payload: str):
        """Called when HA sends a command. Delegates to the monitor."""
        # This will be wired up by PecronMonitor
        if hasattr(self, "command_callback"):
            self.command_callback(device_key, control, payload)

    def _publish_discovery(self):
        """Publish HA MQTT auto-discovery messages."""
        self._published_topics = set()
        # Issue #49: reset before discovery so on_connect's subscribe loop
        # (which runs right after this) sees only currently-registered topics.
        self._command_topics = []
        self._clear_current_discovery = bool(self.clear_discovery_on_startup)
        for device in self.devices:
            dk = device["device_key"]
            name = device["device_name"]

            # Determine device model type
            name_upper = name.upper()
            is_wb = "WB" in name_upper  # WB12200 battery module
            is_pps = not is_wb  # Portable power station (E-series, F-series)

            dev_info = {
                "identifiers": [f"pecron_{dk}"],
                "name": f"Pecron {name} ({dk})",
                "manufacturer": "Pecron",
                "model": name,
                "serial_number": dk,
            }

            # Battery sensor
            self._pub_config(
                "sensor",
                dk,
                "battery",
                {
                    "name": "Battery (SOC)",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.soc_percent }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_battery",
                },
            )

            # Host pack battery sensor
            self._pub_config(
                "sensor",
                dk,
                "host_battery",
                {
                    "name": "Host Battery",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.host_percent }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_host_battery",
                },
            )

            # Voltage sensor
            self._pub_config(
                "sensor",
                dk,
                "voltage",
                {
                    "name": "Voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_voltage",
                },
            )

            # Temperature sensor (primary/host pack temp)
            self._pub_config(
                "sensor",
                dk,
                "temperature",
                {
                    "name": "Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.temperature }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_temperature",
                },
            )

            # E3800-specific temperature sensors. Gated on TSL presence so
            # models that don't expose these properties (E1500, etc.) don't
            # show permanent 'Unknown' entities in HA (issue #35).
            if is_pps and self._has(device, "battery_temp"):
                self._pub_config(
                    "sensor",
                    dk,
                    "battery_temp",
                    {
                        "name": "Battery Temperature",
                        "device_class": "temperature",
                        "unit_of_measurement": "°C",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.battery_temp }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_battery_temp",
                    },
                )

            if is_pps and self._has(device, "charging_plate_temp"):
                self._pub_config(
                    "sensor",
                    dk,
                    "charging_plate_temp",
                    {
                        "name": "Charging Plate Temperature",
                        "device_class": "temperature",
                        "unit_of_measurement": "°C",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.charging_plate_temp }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_charging_plate_temp",
                    },
                )

            if is_pps and self._has(device, "inverter_temp"):
                self._pub_config(
                    "sensor",
                    dk,
                    "inverter_temp",
                    {
                        "name": "Inverter Temperature",
                        "device_class": "temperature",
                        "unit_of_measurement": "°C",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.inverter_temp }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_inverter_temp",
                    },
                )

            # Power in/out sensors
            for key, label in [("total_input", "Input Power"), ("total_output", "Output Power")]:
                self._pub_config(
                    "sensor",
                    dk,
                    key,
                    {
                        "name": label,
                        "device_class": "power",
                        "unit_of_measurement": "W",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": f"{{{{ value_json.{key}_power }}}}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_{key}",
                    },
                )

            # AC input power sensor (separate from DC)
            if is_pps:
                self._pub_config(
                    "sensor",
                    dk,
                    "ac_input",
                    {
                        "name": "AC Input Power",
                        "device_class": "power",
                        "unit_of_measurement": "W",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.ac_input_power }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_ac_input",
                    },
                )

                # DC input power sensor (separate from AC)
                self._pub_config(
                    "sensor",
                    dk,
                    "dc_input",
                    {
                        "name": "DC Input Power",
                        "device_class": "power",
                        "unit_of_measurement": "W",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.dc_input_power }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_dc_input",
                    },
                )

            # Remaining time sensor
            # Remaining time sensor (H:M)
            self._pub_config(
                "sensor",
                dk,
                "remaining_time",
                {
                    "name": "Remaining Time",
                    "icon": "mdi:timer-outline",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.remain_hm }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_remaining_time",
                },
            )

            # AC / DC / UPS output switches. Each has both a command_topic
            # (HA pushes ON/OFF when the user toggles) AND a state_topic with
            # value_template (HA reflects the actual device state from the
            # published state JSON). Not optimistic: the device reports real
            # switch state so HA should show reality, not the last command.
            self._pub_config(
                "switch",
                dk,
                "ac",
                {
                    "name": "AC Output",
                    "icon": "mdi:power-plug",
                    "command_topic": f"pecron/{dk}/ac/set",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_switch }}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac",
                },
            )

            self._pub_config(
                "switch",
                dk,
                "dc",
                {
                    "name": "DC Output",
                    "icon": "mdi:usb-port",
                    "command_topic": f"pecron/{dk}/dc/set",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.dc_switch }}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_dc",
                },
            )

            self._pub_config(
                "switch",
                dk,
                "ups",
                {
                    "name": "UPS Mode",
                    "icon": "mdi:shield-battery",
                    "command_topic": f"pecron/{dk}/ups/set",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ups_mode }}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ups",
                },
            )

            # === E3800-specific automation controls ===
            # These only appear if the device has these capabilities
            # (HA will just show "unavailable" if the device doesn't report them)

            if is_pps:
                # Eco/Quiet mode switch (E3800 TSL has eco_quite_mode_as; E1500 does not)
                if self._has(device, "eco_quite_mode_as"):
                    self._pub_config(
                        "switch",
                        dk,
                        "eco_mode",
                        {
                            "name": "Eco Mode",
                            "icon": "mdi:leaf",
                            "command_topic": f"pecron/{dk}/eco_mode/set",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": "{{ value_json.eco_quite_mode_as }}",
                            "state_on": "ON",
                            "state_off": "OFF",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_eco_mode",
                        },
                    )

                # Touch panel lock (E3800-only)
                if self._has(device, "device_touch_locking_as"):
                    self._pub_config(
                        "switch",
                        dk,
                        "touch_lock",
                        {
                            "name": "Touch Panel Lock",
                            "icon": "mdi:lock",
                            "command_topic": f"pecron/{dk}/touch_lock/set",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": "{{ value_json.device_touch_locking_as }}",
                            "state_on": "ON",
                            "state_off": "OFF",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_touch_lock",
                        },
                    )

                # AC charging power level (E3800-only)
                if self._has(device, "ac_charging_power_ios"):
                    self._pub_config(
                        "select",
                        dk,
                        "ac_charging_power",
                        {
                            "name": "AC Charging Power Setting",
                            "icon": "mdi:flash",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": "{{ (value_json.ac_charging_power_ios | int * 10) ~ '%' if value_json.ac_charging_power_ios is not none else none }}",
                            "options": [
                                "0%",
                                "10%",
                                "20%",
                                "30%",
                                "40%",
                                "50%",
                                "60%",
                                "70%",
                                "80%",
                                "90%",
                                "100%",
                            ],
                            "command_topic": f"pecron/{dk}/ac_charging_power/set",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_ac_charging_power",
                        },
                    )

                # UPS charge threshold (E3800-only)
                if self._has(device, "ups_start_charge_value_as"):
                    self._pub_config(
                        "select",
                        dk,
                        "ups_charge_threshold",
                        {
                            "name": "UPS Charge Threshold",
                            "icon": "mdi:battery-charging",
                            "unit_of_measurement": "%",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": "{{ value_json.ups_start_charge_value_as ~ '%' if value_json.ups_start_charge_value_as is not none else none }}",
                            "options": ["30%", "40%", "50%", "60%", "70%", "80%", "90%", "100%"],
                            "command_topic": f"pecron/{dk}/ups_charge_threshold/set",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_ups_charge_threshold",
                        },
                    )

                # Standby timeout (WB12200 + E3800 TSL have device_standy_times_as; E1500 does not)
                if self._has(device, "device_standy_times_as"):
                    self._pub_config(
                        "sensor",
                        dk,
                        "standby_timeout",
                        {
                            "name": "Standby Timeout",
                            "icon": "mdi:timer-sand",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": "{{ value_json.device_standy_times_as }}",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_standby_timeout",
                        },
                    )

                # Bypass mode (bypass_enable isn't a standard TSL code on any model
                # I've seen; leaving ungated for now, will gate if someone identifies
                # the underlying TSL resource)
                self._pub_config(
                    "switch",
                    dk,
                    "bypass",
                    {
                        "name": "Bypass Mode",
                        "icon": "mdi:transfer",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.bypass_enable }}",
                        "state_on": "ON",
                        "state_off": "OFF",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_bypass",
                    },
                )

            if is_pps:
                # AC output power sensor
                self._pub_config(
                    "sensor",
                    dk,
                    "ac_output",
                    {
                        "name": "AC Output Power",
                        "device_class": "power",
                        "unit_of_measurement": "W",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.ac_output_power }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_ac_output",
                    },
                )

                # AC output voltage sensor
                self._pub_config(
                    "sensor",
                    dk,
                    "ac_output_voltage",
                    {
                        "name": "AC Output Voltage",
                        "device_class": "voltage",
                        "unit_of_measurement": "V",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.ac_output_voltage }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_ac_output_voltage",
                    },
                )

                # AC output frequency sensor
                self._pub_config(
                    "sensor",
                    dk,
                    "ac_output_hz",
                    {
                        "name": "AC Output Frequency",
                        "icon": "mdi:sine-wave",
                        "unit_of_measurement": "Hz",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.ac_output_hz }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_ac_output_hz",
                    },
                )

                # AC power factor sensor
                self._pub_config(
                    "sensor",
                    dk,
                    "ac_output_pf",
                    {
                        "name": "AC Power Factor",
                        "icon": "mdi:angle-acute",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.ac_output_pf }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_ac_output_pf",
                    },
                )

            # Current sensor (amps — critical for RV/motorhome monitoring)
            self._pub_config(
                "sensor",
                dk,
                "current",
                {
                    "name": "Current",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.current }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_current",
                },
            )

            if is_pps:
                # DC output power sensor
                self._pub_config(
                    "sensor",
                    dk,
                    "dc_output",
                    {
                        "name": "DC Output Power",
                        "device_class": "power",
                        "unit_of_measurement": "W",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.dc_output_power }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_dc_output",
                    },
                )

            # Per-port DC input sensors (DC5521 barrel, GX16-MF1/2 solar).
            # NOT published here. Deferred to publish_state so we only register
            # entities for ports that actually exist on the device. Models
            # without per-port breakdown (E1500LFP) used to get 9 ghost
            # entities all stuck at Unknown forever. Now discovery for a port
            # fires the first time the device reports any value for that port.
            # Dev info and is_pps are captured so _ensure_port_discovery can
            # reconstruct the discovery payload later without re-deriving them.
            if is_pps:
                self._device_dev_info[dk] = dev_info

            # Remaining charging time
            self._pub_config(
                "sensor",
                dk,
                "remaining_charging_time",
                {
                    "name": "Remaining Charging Time",
                    "icon": "mdi:battery-clock",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.remain_charging_hm }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_remaining_charging_time",
                },
            )

            # Total energy (cumulative PV generation). Only on models with
            # PV input reporting (E3800, F-series). E1500 and WB12200 do
            # not expose total_energy in their TSL (issue #35).
            if self._has(device, "total_energy"):
                self._pub_config(
                    "sensor",
                    dk,
                    "total_energy",
                    {
                        "name": "Total PV Energy",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "kWh",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.total_energy }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_total_energy",
                    },
                )

            # Device status
            self._pub_config(
                "sensor",
                dk,
                "device_status",
                {
                    "name": "Device Status",
                    "icon": "mdi:battery-sync",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.device_status_hm }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_device_status",
                },
            )

            # Expansion battery pack status
            self._pub_config(
                "binary_sensor",
                dk,
                "expansion_pack",
                {
                    "name": "Expansion Pack",
                    "device_class": "connectivity",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.add_bat_status_hm }}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_expansion_pack",
                },
            )

            # Auto-dim on idle switch
            self._pub_config(
                "switch",
                dk,
                "auto_dim",
                {
                    "name": "Auto-Dim on Idle",
                    "icon": "mdi:brightness-auto",
                    "command_topic": f"pecron/{dk}/auto_light_flag_as/set",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.auto_light_flag_as }}",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_auto_dim",
                },
            )

            # Screen brightness level
            self._pub_config(
                "sensor",
                dk,
                "screen_brightness",
                {
                    "name": "Screen Brightness",
                    "icon": "mdi:brightness-6",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.machine_screen_light_as }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_screen_brightness",
                },
            )

            # AC output voltage setting
            self._pub_config(
                "sensor",
                dk,
                "ac_voltage_setting",
                {
                    "name": "AC Output Voltage Setting",
                    "icon": "mdi:flash",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_output_voltage_io }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_voltage_setting",
                },
            )

            # AC output frequency setting
            self._pub_config(
                "sensor",
                dk,
                "ac_frequency_setting",
                {
                    "name": "AC Output Frequency Setting",
                    "icon": "mdi:sine-wave",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_output_frequency_io }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_frequency_setting",
                },
            )

            # No-output auto-off timer
            self._pub_config(
                "sensor",
                dk,
                "auto_off_timer",
                {
                    "name": "No-Output Auto-Off Timer",
                    "icon": "mdi:timer-off",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.noastime_io }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_auto_off_timer",
                },
            )

            # === WB12200 battery management sensors ===
            if is_wb:
                self._pub_config(
                    "sensor",
                    dk,
                    "charging_limit_voltage",
                    {
                        "name": "Charging Limit Voltage",
                        "device_class": "voltage",
                        "icon": "mdi:battery-charging-high",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.charging_limit_voltage }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_charging_limit_voltage",
                    },
                )

                self._pub_config(
                    "sensor",
                    dk,
                    "discharge_limit_voltage",
                    {
                        "name": "Discharge Limit Voltage",
                        "device_class": "voltage",
                        "icon": "mdi:battery-low",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.discharge_limiting_voltage }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_discharge_limit_voltage",
                    },
                )

                self._pub_config(
                    "sensor",
                    dk,
                    "charging_current_limit",
                    {
                        "name": "Charging Current Limit",
                        "device_class": "current",
                        "unit_of_measurement": "A",
                        "icon": "mdi:current-dc",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.charging_current_limit }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_charging_current_limit",
                    },
                )

                self._pub_config(
                    "sensor",
                    dk,
                    "discharge_current_limit",
                    {
                        "name": "Discharge Current Limit",
                        "device_class": "current",
                        "unit_of_measurement": "A",
                        "icon": "mdi:current-dc",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.discharge_limiting_current }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_discharge_current_limit",
                    },
                )

                self._pub_config(
                    "sensor",
                    dk,
                    "battery_heating",
                    {
                        "name": "Battery Heating Mode",
                        "icon": "mdi:radiator",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.battery_heating_mode }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_battery_heating",
                    },
                )

                # Beep/voice switch (WB12200)
                self._pub_config(
                    "switch",
                    dk,
                    "beep",
                    {
                        "name": "Beep/Voice Alert",
                        "icon": "mdi:volume-high",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.beep_voice_us }}",
                        "state_on": "ON",
                        "state_off": "OFF",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_beep",
                    },
                )

                # Fault alarm sensor
                self._pub_config(
                    "sensor",
                    dk,
                    "fault_alarm",
                    {
                        "name": "Fault Alarm",
                        "icon": "mdi:alert-circle",
                        "state_topic": f"pecron/{dk}/state",
                        "value_template": "{{ value_json.FAULT_ALARM_ENUM }}",
                        "device": dev_info,
                        "unique_id": f"pecron_{dk}_fault_alarm",
                    },
                )

            # Per-pack sensors (charging_pack_data_jdb) — packs 0-3 (PPS only)
            if is_pps:
                for pack_num in range(4):
                    # Pack battery percentage
                    self._pub_config(
                        "sensor",
                        dk,
                        f"pack_{pack_num}_battery",
                        {
                            "name": f"Pack {pack_num} Battery",
                            "device_class": "battery",
                            "unit_of_measurement": "%",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": f"{{{{ value_json.pack_{pack_num}_battery }}}}",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_pack_{pack_num}_battery",
                        },
                    )

                    # Pack voltage
                    self._pub_config(
                        "sensor",
                        dk,
                        f"pack_{pack_num}_voltage",
                        {
                            "name": f"Pack {pack_num} Voltage",
                            "device_class": "voltage",
                            "unit_of_measurement": "V",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": f"{{{{ value_json.pack_{pack_num}_voltage }}}}",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_pack_{pack_num}_voltage",
                        },
                    )

                    # Pack current
                    self._pub_config(
                        "sensor",
                        dk,
                        f"pack_{pack_num}_current",
                        {
                            "name": f"Pack {pack_num} Current",
                            "device_class": "current",
                            "unit_of_measurement": "A",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": f"{{{{ value_json.pack_{pack_num}_current }}}}",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_pack_{pack_num}_current",
                        },
                    )

                    # Pack temperature
                    self._pub_config(
                        "sensor",
                        dk,
                        f"pack_{pack_num}_temp",
                        {
                            "name": f"Pack {pack_num} Temperature",
                            "device_class": "temperature",
                            "unit_of_measurement": "°C",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": f"{{{{ value_json.pack_{pack_num}_temp }}}}",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_pack_{pack_num}_temp",
                        },
                    )

                    # Pack status
                    self._pub_config(
                        "sensor",
                        dk,
                        f"pack_{pack_num}_status",
                        {
                            "name": f"Pack {pack_num} Status",
                            "icon": "mdi:battery-sync",
                            "state_topic": f"pecron/{dk}/state",
                            "value_template": f"{{{{ value_json.pack_{pack_num}_status }}}}",
                            "device": dev_info,
                            "unique_id": f"pecron_{dk}_pack_{pack_num}_status",
                        },
                    )

            # Clear stale entities from previous versions that no longer apply to this model
            self._clear_stale_entities(dk)
        self._clear_current_discovery = False

        log.info("Published Home Assistant discovery configs")

    @staticmethod
    def _has(device: dict, *resource_codes: str) -> bool:
        """Return True if the device's TSL cache includes at least one of the
        named resource codes. Used to gate HA discovery publication so models
        that lack a TSL property don't end up with perpetual 'Unknown' entities
        (issue #35). Pass multiple codes when a single entity maps to whichever
        one the firmware exposes (e.g. 'battery_percentage' vs 'battery').

        Logs at DEBUG when gating skips a code so users who run with --verbose
        can see why an entity they expected isn't appearing. The suggested
        remediation is to re-run --setup which refreshes the TSL cache from
        the Pecron cloud (new firmware sometimes adds resource codes).
        """
        controls = device.get("controls", {}) or {}
        has = any(code in controls for code in resource_codes)
        if not has:
            log.debug(
                "TSL gate: device %s lacks %s, skipping entity publish "
                "(re-run --setup if this should be present)",
                device.get("device_key", "?"),
                " or ".join(resource_codes),
            )
        return has

    # Port names -> HA-friendly display prefix + per-suffix label style.
    # Keys match the prefix pecron-monitor emits; values are (base_label,
    # suffix_separator) where suffix_separator is " Input " for DC5521 and
    # " " for solar ports, matching the naming that shipped pre-0.7.5.
    _PORT_LABELS = {
        "dc5521": ("DC5521", " Input "),
        "gx16mf1": ("Solar Port 1", " "),
        "gx16mf2": ("Solar Port 2", " "),
    }

    def _ensure_port_discovery(self, dk: str, port: str):
        """Publish discovery for a per-port DC-input port on first observation.
        No-op if already published for this (device, port) pair since the bridge
        last connected. Solves the ghost-entity problem where models without
        per-port breakdown would still get discovery for DC5521 + GX16 ports."""
        if (dk, port) in self._deferred_ports_published:
            return
        dev_info = self._device_dev_info.get(dk)
        if dev_info is None:
            # Called before _publish_discovery captured dev_info; skip and retry next call.
            return

        base_label, sep = self._PORT_LABELS.get(port, (port.upper(), " "))
        for suffix, dev_class, unit, display_suffix in [
            ("voltage", "voltage", "V", "Voltage"),
            ("current", "current", "A", "Current"),
            ("power", "power", "W", "Power"),
        ]:
            key = f"{port}_input_{suffix}"
            self._pub_config(
                "sensor",
                dk,
                key,
                {
                    "name": f"{base_label}{sep}{display_suffix}",
                    "device_class": dev_class,
                    "unit_of_measurement": unit,
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_{key}",
                },
            )
        self._deferred_ports_published.add((dk, port))
        log.info("Registered HA discovery for '%s' port on %s (first observation)", base_label, dk)

    def _pub_config(self, component: str, dk: str, key: str, config: dict):
        # Issue #34: collapse non-essential entities under HA's Configuration /
        # Diagnostic sections. Applied here so every call site benefits without
        # 50 per-call-site edits.
        category = entity_category_for(key)
        if component == "sensor" and category == "config":
            # Home Assistant rejects config-category sensors. Keep these
            # rarely-used settings out of the main view by downgrading them to
            # diagnostic rather than publishing invalid discovery payloads.
            category = "diagnostic"
        if category and "entity_category" not in config:
            config = {**config, "entity_category": category}
        # Issue #78: tag instantaneous measurement sensors with
        # state_class=measurement so HA records long-term statistics for them.
        # Applied centrally so every numeric sensor benefits without per-call-site
        # edits. Call sites that set state_class explicitly (e.g. total_energy =>
        # total_increasing) are left untouched. _LABEL_SENSOR_KEYS are excluded:
        # they carry a numeric device_class but publish string labels, invalid
        # under a numeric state_class.
        if (
            component == "sensor"
            and key not in _LABEL_SENSOR_KEYS
            and "state_class" not in config
            and config.get("device_class") in _MEASUREMENT_DEVICE_CLASSES
        ):
            config = {**config, "state_class": "measurement"}
        topic = f"{self.discovery_prefix}/{component}/pecron_{dk}/{key}/config"
        if self._clear_current_discovery:
            self.client.publish(topic, "", qos=1, retain=True)
        self.client.publish(topic, json.dumps(config), qos=1, retain=True)
        self._published_topics.add(topic)
        # Issue #49: capture every command_topic this entity registers so the
        # MQTT subscribe step in on_connect stays in lockstep with discovery.
        cmd_topic = config.get("command_topic")
        if cmd_topic and cmd_topic not in self._command_topics:
            self._command_topics.append(cmd_topic)

    def _clear_stale_entities(self, dk: str):
        """Publish empty retained messages for entities that were previously published
        but are no longer relevant (e.g., WB12200-only entities on an E3800).
        This removes stale entities from Home Assistant."""
        # All possible entity keys across all models
        ALL_ENTITY_KEYS = {
            "sensor": [
                "battery",
                "host_battery",
                "voltage",
                "temperature",
                "current",
                "total_input",
                "total_output",
                "remaining_time",
                "battery_temp",
                "charging_plate_temp",
                "inverter_temp",
                "ac_input",
                "dc_input",
                "ac_output",
                "dc_output",
                "ac_output_voltage",
                "ac_output_hz",
                "ac_output_pf",
                "dc5521_input_voltage",
                "dc5521_input_current",
                "dc5521_input_power",
                "gx16mf1_input_voltage",
                "gx16mf1_input_current",
                "gx16mf1_input_power",
                "gx16mf2_input_voltage",
                "gx16mf2_input_current",
                "gx16mf2_input_power",
                "remaining_charging_time",
                "total_energy",
                "device_status",
                "ac_charging_power",
                "ups_charge_threshold",
                "standby_timeout",
                "screen_brightness",
                "auto_off_timer",
                "ac_voltage_setting",
                "ac_frequency_setting",
                "charging_limit_voltage",
                "discharge_limit_voltage",
                "charging_current_limit",
                "discharge_current_limit",
                "battery_heating",
                "fault_alarm",
            ]
            + [
                f"pack_{i}_{f}"
                for i in range(4)
                for f in ["battery", "voltage", "current", "temp", "status"]
            ],
            "switch": ["ac", "dc", "ups", "eco_mode", "touch_lock", "bypass", "auto_dim", "beep"],
            "binary_sensor": ["expansion_pack"],
            "select": ["ac_charging_power", "ups_charge_threshold"],
        }
        for component, keys in ALL_ENTITY_KEYS.items():
            for key in keys:
                topic = f"{self.discovery_prefix}/{component}/pecron_{dk}/{key}/config"
                if topic not in self._published_topics:
                    # Publish empty retained message to clear stale entity
                    self.client.publish(topic, "", qos=1, retain=True)

    def publish_state(self, device_key: str, kv: dict):
        """Publish current state to HA.

        The device sends multiple payload "shapes" (e.g., host packet vs overall packet).
        Some shapes omit fields and/or carry placeholder zeros; without caching, HA entities
        will flap between valid values and 0/unknown. We therefore merge updates into a
        per-device cache and only overwrite fields when the source field is present.
        """
        if not self._connected:
            return

        cache = self._state_cache.setdefault(device_key, {})

        def _get_first_present(paths):
            """
            Return (present, value) for the first path that exists in this payload shape.
            'present' means the field path resolved to a non-None value (0 is valid).
            """
            for p in paths:
                val = _get_kv_single(kv, p)
                if val is not None:
                    return True, val
            return False, None

        # Identify payload shape (host packet vs overall packet)
        host_dict = kv.get("host_packet_data_jdb")
        packet_has_host = isinstance(host_dict, dict) and bool(host_dict)

        # ---- Core sensors ----
        # For these, only overwrite when their source field exists in the payload shape.
        # Accept 0 as a real reading *only if the source path is present*.

        # Voltage is special: 0 is never a legitimate reading on a live battery
        # pack (the bus voltage is always >0 while the device can respond at all).
        # A 0.0V value in the packet means the packet was a settings-only shape
        # that carried a placeholder, not that voltage actually dropped to zero.
        # Skip the update in that case; HA graphs stop showing spurious dips,
        # the cached last-known-good value stays visible, and real readings
        # always overwrite it as soon as they arrive. Issue #36.
        present, v = _get_first_present(SENSOR_FIELDS["voltage"])
        if present:
            try:
                new_voltage = round(float(v), 1)
                if new_voltage > 0:
                    cache["voltage"] = new_voltage
                # else: keep the cached value if any; leave cache untouched
                # otherwise so HA shows Unknown until a real reading arrives.
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["temperature"])
        if present:
            try:
                cache["temperature"] = int(float(v))
            except (TypeError, ValueError):
                pass

        # E3800-specific temperature sensors
        present, v = _get_first_present(SENSOR_FIELDS["battery_temp"])
        if present:
            try:
                cache["battery_temp"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["charging_plate_temp"])
        if present:
            try:
                cache["charging_plate_temp"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["inverter_temp"])
        if present:
            try:
                cache["inverter_temp"] = int(float(v))
            except (TypeError, ValueError):
                pass

        # Track whether the top-level total fields were present AND accepted
        # in *this* packet (vs only carried over from the cache). The aggregate
        # fallback at the end of publish_state needs this so it can preserve a
        # canonical top-level reading received in this packet on standalone PPS
        # devices, while still re-aggregating when the top-level was absent or
        # was suppressed by the host-zero guard. See issue #48.
        total_input_top_level_used = False
        total_output_top_level_used = False

        present, v = _get_first_present(SENSOR_FIELDS["total_input_power"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["total_input_power"] = int(float(v))
                total_input_top_level_used = True
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["total_output_power"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["total_output_power"] = int(float(v))
                total_output_top_level_used = True
            except (TypeError, ValueError):
                pass

        v = kv.get("total_energy")
        if v is not None:
            try:
                cache["total_energy"] = round(float(v), 3)
            except (TypeError, ValueError):
                pass

        # AC and DC input power (separate sensors for E3800 and others)
        # ALWAYS publish input power values (including 0) — 0W is valid, "Unknown" is not
        present, v = _get_first_present(SENSOR_FIELDS["ac_input_power"])
        if present:
            try:
                cache["ac_input_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["dc_input_power"])
        if present:
            try:
                cache["dc_input_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["remain_time"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["remain_minutes"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["remain_charging_time"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["remain_charging_minutes"] = int(float(v))
            except (TypeError, ValueError):
                pass

        # Human-friendly remaining time for UI
        cache["remain_hm"] = _fmt_dhm(cache.get("remain_minutes"))
        cache["remain_charging_hm"] = _fmt_dhm(cache.get("remain_charging_minutes"))

        # ---- Switch states ----
        # Some payloads don't include these; cache last known.
        def _update_switch(field_key, out_key):
            present, v = _get_first_present(SENSOR_FIELDS[field_key])
            if present:
                cache[out_key] = "ON" if _truthy(v) else "OFF"

        _update_switch("ac_switch", "ac_switch")
        _update_switch("dc_switch", "dc_switch")
        _update_switch("ups_mode", "ups_mode")
        _update_switch("add_bat_status_hm", "add_bat_status_hm")

        # ---- E3800 automation controls ----
        for field in (
            "eco_quite_mode_as",
            "device_touch_locking_as",
            "bypass_enable",
            "auto_light_flag_as",
        ):
            v = kv.get(field)
            if v is not None:
                cache[field] = "ON" if _truthy(v) else "OFF"

        for field in (
            "ac_charging_power_ios",
            "ups_start_charge_value_as",
            "device_standy_times_as",
            "machine_screen_light_as",
            "ac_output_voltage_io",
            "ac_output_frequency_io",
            "noastime_io",
        ):
            v = kv.get(field)
            if v is not None:
                cache[field] = v

        # ---- WB12200 battery management (decode enum indices to friendly labels) ----
        _wb_enum_fields = {
            "charging_limit_voltage": WB_CHARGE_VOLTAGE_LABELS,
            "discharge_limiting_voltage": WB_DISCHARGE_VOLTAGE_LABELS,
            "charging_current_limit": WB_CHARGE_CURRENT_LABELS,
            "discharge_limiting_current": WB_DISCHARGE_CURRENT_LABELS,
            "battery_heating_mode": WB_HEATING_MODE_LABELS,
        }
        for field, labels in _wb_enum_fields.items():
            v = kv.get(field)
            if v is not None:
                try:
                    cache[field] = labels.get(int(v), str(v))
                except (TypeError, ValueError):
                    cache[field] = str(v)

        v = kv.get("FAULT_ALARM_ENUM")
        if v is not None:
            try:
                cache["FAULT_ALARM_ENUM"] = FAULT_ALARM_LABELS.get(int(v), f"Fault {v}")
            except (TypeError, ValueError):
                cache["FAULT_ALARM_ENUM"] = str(v)

        for field in ("beep_voice_us", "battery_indicator_us"):
            v = kv.get(field)
            if v is not None:
                cache[field] = "ON" if _truthy(v) else "OFF"

        # AC output sensors
        present, v = _get_first_present(SENSOR_FIELDS["ac_output_power"])
        if present:
            try:
                cache["ac_output_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["ac_output_voltage"])
        if present:
            try:
                cache["ac_output_voltage"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["dc_output_power"])
        if present:
            try:
                cache["dc_output_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["device_status_hm"])
        if present:
            try:
                cache["device_status_hm"] = DEVICE_STATUS_LABELS.get(int(v), str(v))
            except (TypeError, ValueError):
                cache["device_status_hm"] = str(v)

        # Issue #45: device_status_hm is carried only by overall-shape packets.
        # On standalone PPS (E1500LFP) that primarily emit host-shape packets
        # during active operation, the cache freezes at the last overall
        # reading — typically "Shut Down" from before the device woke up. HA
        # then reports the device as Shut Down even while it's actively
        # charging or discharging. Detect the contradiction and infer the
        # live status from observed power activity. The next genuine overall
        # packet still wins (we only override when cached == "Shut Down"
        # AND power is flowing). Sibling pattern of the soc_percent fix in
        # PR #44; this field has no host-shape equivalent to mirror, so we
        # derive it from total_input_power / total_output_power /
        # ac_output_power / dc_output_power which host-shape packets DO carry.
        if cache.get("device_status_hm") == "Shut Down":
            total_in = cache.get("total_input_power") or 0
            total_out = cache.get("total_output_power") or 0
            ac_out = cache.get("ac_output_power") or 0
            dc_out = cache.get("dc_output_power") or 0
            if total_in > 0:
                # Power coming in -> charging. Charging takes precedence over
                # simultaneous discharge (passthrough mode reads as charging
                # to HA, which is the user-relevant state).
                cache["device_status_hm"] = "Charging"
            elif total_out > 0:
                # Power going out and none coming in -> discharging. Pick
                # AC vs DC by which output dominates; if neither dominates,
                # leave the cached value alone rather than guess.
                if ac_out > dc_out:
                    cache["device_status_hm"] = "AC Discharge"
                elif dc_out > 0:
                    cache["device_status_hm"] = "DC Discharge"

        # Current (amps)
        present, v = _get_first_present(SENSOR_FIELDS["current"])
        if present:
            try:
                cache["current"] = round(float(v), 2)
            except (TypeError, ValueError):
                pass

        # ---- Per-port DC input sensors (solar + barrel) ----
        # For idle ports the device truthfully reports 0V / 0A / 0W; publish
        # those zeros through. That's distinct from the pack case above where
        # disconnected slots bleed misleading host-pack data into slot 0.
        # A real zero is honest; a null/Unknown there would be worse UX.
        #
        # Discovery for each port is deferred (see _ensure_port_discovery):
        # a port's three HA entities are only registered the first time the
        # device reports any data for that port. Models without the hardware
        # (E1500LFP) never trigger discovery and therefore don't accumulate
        # ghost Unknown entities.
        for port in ("dc5521", "gx16mf1", "gx16mf2"):
            port_reported = False
            for field_suffix, rounding in [("voltage", 1), ("current", 2), ("power", 0)]:
                field = f"{port}_input_{field_suffix}"
                present, v = _get_first_present(SENSOR_FIELDS[field])
                if present:
                    port_reported = True
                    try:
                        cache[field] = round(float(v), rounding) if rounding else int(float(v))
                    except (TypeError, ValueError):
                        pass
            if port_reported:
                self._ensure_port_discovery(device_key, port)

        # ---- AC output actual readings ----
        present, v = _get_first_present(SENSOR_FIELDS["ac_output_hz"])
        if present:
            try:
                cache["ac_output_hz"] = round(float(v), 1)
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["ac_output_pf"])
        if present:
            try:
                cache["ac_output_pf"] = round(float(v), 2)
            except (TypeError, ValueError):
                pass

        # ---- Per-pack sensors (charging_pack_data_jdb) ----
        # Pack status enum: 0=no charge, 1=cascade charging, 2=balance no charge,
        # 3=balanced charging, 4=no connection. Slots reporting "no connection"
        # are unoccupied expansion-pack bays on standalone PPS. Publishing their
        # zeroed battery/voltage/current/temp values pollutes HA with ghost
        # entities that can never have real data; skip them so the state JSON
        # omits those keys and HA's template returns None (Unknown).
        packs = kv.get("charging_pack_data_jdb", [])
        if isinstance(packs, list):
            for i, pack in enumerate(packs[:4]):
                if not isinstance(pack, dict):
                    continue
                try:
                    status_val = int(float(pack.get("charging_pack_status", 4)))
                except (TypeError, ValueError):
                    status_val = 4

                if status_val == 4:
                    # Unoccupied slot. Publish explicit JSON null rather than
                    # omitting the keys: HA's value_template returns Undefined
                    # for missing keys and keeps the last known state, which
                    # leaves stale values visible after a reload. null /
                    # Jinja None transitions the entity cleanly to Unknown.
                    for suffix in ("status", "battery", "voltage", "current", "temp"):
                        cache[f"pack_{i}_{suffix}"] = None
                    continue

                cache[f"pack_{i}_status"] = PACK_STATUS_LABELS.get(status_val, str(status_val))

                try:
                    bat = int(float(pack.get("charging_pack_battery", 0)))
                    # Apply same swap fix: if battery=0 and status looks like a percentage
                    if bat == 0 and 5 <= status_val <= 100:
                        bat = status_val
                    cache[f"pack_{i}_battery"] = bat
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_voltage"] = round(
                        float(pack.get("charging_pack_voltage", 0)), 1
                    )
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_current"] = round(
                        float(pack.get("charging_pack_current", 0)), 2
                    )
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_temp"] = int(float(pack.get("charging_pack_temp", 0)))
                except (TypeError, ValueError):
                    pass

        # Fallback aggregate: on some models (E3800LFP, E1500LFP) the top-level
        # total_input_power / total_output_power fields are never populated in
        # the MQTT packets, but the per-source ac_input_power / dc_input_power
        # (and ac_output_power / dc_output_power) always are. Compute the total
        # from components when the top-level total isn't in cache so HA's Input
        # Power / Output Power entities don't sit at Unknown indefinitely.
        # Parallels the same fallback already done in monitor._process_data for
        # the status log. Runs AFTER all components are cached so it sees
        # whatever landed in this or any earlier packet.
        #
        # Issue #48: on standalone PPS (no occupied expansion packs) the same
        # "fill once, never refresh" pattern that #43 hit for soc_percent also
        # hits the totals. The host-zero guard above on total_input_power /
        # total_output_power skips the 0 reading on host packets, so when AC
        # is unplugged the cached value stays non-zero (e.g. 1329W) forever
        # while ac_input_power and dc_input_power correctly read 0; the old
        # `if total is None` fallback never re-runs because the cache is
        # already populated. Detect standalone the same way #44 did (no
        # pack_*_status set) and re-aggregate from components for those
        # devices when the top-level total was NOT explicitly accepted from
        # this packet (i.e. either absent or suppressed by the host-zero
        # guard). If the device DID send a real top-level total this packet,
        # respect it -- some models include components other than ac+dc in
        # their top-level reading. Devices with packs preserve the original
        # "fill once, never refresh" behavior unconditionally.
        is_standalone = not any(cache.get(f"pack_{i}_status") for i in range(4))

        ac_in = cache.get("ac_input_power")
        dc_in = cache.get("dc_input_power")
        should_aggregate_input = cache.get("total_input_power") is None or (
            is_standalone and not total_input_top_level_used
        )
        if should_aggregate_input and ac_in is not None and dc_in is not None:
            cache["total_input_power"] = int(ac_in + dc_in)

        ac_out = cache.get("ac_output_power")
        dc_out = cache.get("dc_output_power")
        should_aggregate_output = cache.get("total_output_power") is None or (
            is_standalone and not total_output_top_level_used
        )
        if should_aggregate_output and ac_out is not None and dc_out is not None:
            cache["total_output_power"] = int(ac_out + dc_out)

        # ---- SOC vs Host % ----
        # Your device alternates two payload shapes:
        #   * host packet (has host_packet_data_jdb.*) -> host %
        #   * overall packet (no host_packet_data_jdb) -> overall SOC %
        #
        # IMPORTANT: when host_packet_data_jdb is present, battery_percentage mirrors host %,
        # so we *must not* treat it as SOC in that shape.
        if packet_has_host:
            present, v = _get_first_present(
                [("host_packet_data_jdb", "host_packet_electric_percentage")]
            )
            if present:
                try:
                    cache["host_percent"] = int(float(v))
                except (TypeError, ValueError):
                    pass
        else:
            present, v = _get_first_present([("battery_percentage",)])
            if present:
                try:
                    cache["soc_percent"] = int(float(v))
                except (TypeError, ValueError):
                    pass

        # SOC fallback: on standalone PPS (no occupied expansion packs) the
        # overall SOC and host % are by definition the same number, so mirror
        # host_percent into soc_percent on every host-shape packet so HA tracks
        # live state. Without this, a single "overall" packet that happened to
        # arrive at a stale value (e.g. 100% reported just before the device
        # went into shutdown) leaves soc_percent frozen at that value forever
        # while host_percent updates live with every host-shape packet.
        # Devices WITH expansion packs preserve the original "fill once, don't
        # clobber" behavior: their overall SOC and host % can legitimately
        # differ, and the explicit overall-shape reading is canonical.
        # Pack processing runs earlier in this function, so pack_*_status in
        # cache already reflects this packet's pack state when we get here.
        has_pack = any(cache.get(f"pack_{i}_status") for i in range(4))
        if cache.get("host_percent") is not None and (
            not has_pack or cache.get("soc_percent") is None
        ):
            cache["soc_percent"] = cache["host_percent"]

        # Ensure keys exist for HA templates (but don't force unknown -> 0)
        cache.setdefault("host_percent", None)
        cache.setdefault("soc_percent", None)
        cache.setdefault("remain_hm", _fmt_dhm(cache.get("remain_minutes")))
        cache.setdefault("remain_charging_hm", _fmt_dhm(cache.get("remain_charging_minutes")))

        self.client.publish(f"pecron/{device_key}/state", json.dumps(cache), qos=1, retain=True)

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
