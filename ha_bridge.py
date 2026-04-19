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

from helpers import _truthy, _get_kv, _get_kv_single, _fmt_dhm
from constants import (SENSOR_FIELDS, DEVICE_STATUS_LABELS, FAULT_ALARM_LABELS,
                       WB_CHARGE_VOLTAGE_LABELS, WB_DISCHARGE_VOLTAGE_LABELS,
                       WB_CHARGE_CURRENT_LABELS, WB_DISCHARGE_CURRENT_LABELS,
                       WB_HEATING_MODE_LABELS, WB_BATTERY_CODING_LABELS,
                       WB_STANDBY_TIME_LABELS, PACK_STATUS_LABELS)

log = logging.getLogger("pecron")


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

        # Cache last-known-good values per device so partial payloads don't zero-out entities
        self._state_cache = {}  # device_key -> dict of last published fields
        # Cache last-known values per device so partial payloads (host-only vs SOC-only)
        # don't clobber sensors to 0/unknown in Home Assistant.
        self._last_state = {}  # device_key -> dict

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
                # Subscribe to command topics
                for device in self.devices:
                    dk = device["device_key"]
                    for ctrl in ["ac", "dc", "ups"]:
                        client.subscribe(f"pecron/{dk}/{ctrl}/set", qos=1)

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
            log.error("Cannot connect to MQTT broker at %s:%d (%s). Will retry every %ds.",
                      host, port, e, self._retry_interval)
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
        if hasattr(self, 'command_callback'):
            self.command_callback(device_key, control, payload == "ON")

    def _publish_discovery(self):
        """Publish HA MQTT auto-discovery messages."""
        self._published_topics = set()
        for device in self.devices:
            dk = device["device_key"]
            name = device["device_name"]

            # Determine device model type
            name_upper = name.upper()
            is_wb = 'WB' in name_upper  # WB12200 battery module
            is_pps = not is_wb  # Portable power station (E-series, F-series)

            dev_info = {
                "identifiers": [f"pecron_{dk}"],
                "name": f"Pecron {name} ({dk})",
                "manufacturer": "Pecron",
                "model": name,
                "serial_number": dk,
            }

            # Battery sensor
            self._pub_config("sensor", dk, "battery", {
                "name": "Battery (SOC)",
                "device_class": "battery",
                "unit_of_measurement": "%",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.soc_percent }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_battery",
            })


            # Host pack battery sensor
            self._pub_config("sensor", dk, "host_battery", {
                "name": "Host Battery",
                "device_class": "battery",
                "unit_of_measurement": "%",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.host_percent }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_host_battery",
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

            # Temperature sensor (primary/host pack temp)
            self._pub_config("sensor", dk, "temperature", {
                "name": "Temperature",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.temperature }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_temperature",
            })

            # E3800-specific temperature sensors (3 separate sensors)
            if is_pps:
                self._pub_config("sensor", dk, "battery_temp", {
                    "name": "Battery Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.battery_temp }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_battery_temp",
                })

                self._pub_config("sensor", dk, "charging_plate_temp", {
                    "name": "Charging Plate Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.charging_plate_temp }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_charging_plate_temp",
                })

                self._pub_config("sensor", dk, "inverter_temp", {
                    "name": "Inverter Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.inverter_temp }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_inverter_temp",
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

            # AC input power sensor (separate from DC)
            if is_pps:
                self._pub_config("sensor", dk, "ac_input", {
                    "name": "AC Input Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_input_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_input",
                })

                # DC input power sensor (separate from AC)
                self._pub_config("sensor", dk, "dc_input", {
                    "name": "DC Input Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.dc_input_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_dc_input",
                })

            # Remaining time sensor
            # Remaining time sensor (H:M)
            self._pub_config("sensor", dk, "remaining_time", {
                "name": "Remaining Time",
                "icon": "mdi:timer-outline",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.remain_hm }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_remaining_time",
            })

            # AC switch
            self._pub_config("switch", dk, "ac", {
                "name": "AC Output",
                "icon": "mdi:power-plug",
                "command_topic": f"pecron/{dk}/ac/set",
                "optimistic": True,
                "assumed_state": True,
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_ac",
            })

            # DC switch
            self._pub_config("switch", dk, "dc", {
                "name": "DC Output",
                "icon": "mdi:usb-port",
                "command_topic": f"pecron/{dk}/dc/set",
                "optimistic": True,
                "assumed_state": True,
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_dc",
            })

            # UPS switch
            self._pub_config("switch", dk, "ups", {
                "name": "UPS Mode",
                "icon": "mdi:shield-battery",
                "command_topic": f"pecron/{dk}/ups/set",
                "optimistic": True,
                "assumed_state": True,
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_ups",
            })

            # === E3800-specific automation controls ===
            # These only appear if the device has these capabilities
            # (HA will just show "unavailable" if the device doesn't report them)

            if is_pps:
                # Eco/Quiet mode switch
                self._pub_config("switch", dk, "eco_mode", {
                    "name": "Eco Mode",
                    "icon": "mdi:leaf",
                    "command_topic": f"pecron/{dk}/eco_mode/set",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.eco_quite_mode_as }}",
                    "state_on": "ON", "state_off": "OFF",
                    "payload_on": "ON", "payload_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_eco_mode",
                })

                # Touch panel lock
                self._pub_config("switch", dk, "touch_lock", {
                    "name": "Touch Panel Lock",
                    "icon": "mdi:lock",
                    "command_topic": f"pecron/{dk}/touch_lock/set",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.device_touch_locking_as }}",
                    "state_on": "ON", "state_off": "OFF",
                    "payload_on": "ON", "payload_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_touch_lock",
                })

                # AC charging power level
                self._pub_config("sensor", dk, "ac_charging_power", {
                    "name": "AC Charging Power Setting",
                    "icon": "mdi:flash",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_charging_power_ios }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_charging_power",
                })

                # UPS charge threshold
                self._pub_config("sensor", dk, "ups_charge_threshold", {
                    "name": "UPS Charge Threshold",
                    "icon": "mdi:battery-charging",
                    "unit_of_measurement": "%",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ups_start_charge_value_as }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ups_charge_threshold",
                })

                # Standby timeout
                self._pub_config("sensor", dk, "standby_timeout", {
                    "name": "Standby Timeout",
                    "icon": "mdi:timer-sand",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.device_standy_times_as }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_standby_timeout",
                })

                # Bypass mode
                self._pub_config("switch", dk, "bypass", {
                    "name": "Bypass Mode",
                    "icon": "mdi:transfer",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.bypass_enable }}",
                    "state_on": "ON", "state_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_bypass",
                })

            if is_pps:
                # AC output power sensor
                self._pub_config("sensor", dk, "ac_output", {
                    "name": "AC Output Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_output_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_output",
                })

                # AC output voltage sensor
                self._pub_config("sensor", dk, "ac_output_voltage", {
                    "name": "AC Output Voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_output_voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_output_voltage",
                })

                # AC output frequency sensor
                self._pub_config("sensor", dk, "ac_output_hz", {
                    "name": "AC Output Frequency",
                    "icon": "mdi:sine-wave",
                    "unit_of_measurement": "Hz",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_output_hz }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_output_hz",
                })

                # AC power factor sensor
                self._pub_config("sensor", dk, "ac_output_pf", {
                    "name": "AC Power Factor",
                    "icon": "mdi:angle-acute",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.ac_output_pf }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_ac_output_pf",
                })

            # Current sensor (amps — critical for RV/motorhome monitoring)
            self._pub_config("sensor", dk, "current", {
                "name": "Current",
                "device_class": "current",
                "unit_of_measurement": "A",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.current }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_current",
            })

            if is_pps:
                # DC output power sensor
                self._pub_config("sensor", dk, "dc_output", {
                    "name": "DC Output Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.dc_output_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_dc_output",
                })

            if is_pps:
                # DC5521 (barrel) input sensors
                self._pub_config("sensor", dk, "dc5521_input_voltage", {
                    "name": "DC5521 Input Voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.dc5521_input_voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_dc5521_input_voltage",
                })

                self._pub_config("sensor", dk, "dc5521_input_current", {
                    "name": "DC5521 Input Current",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.dc5521_input_current }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_dc5521_input_current",
                })

                self._pub_config("sensor", dk, "dc5521_input_power", {
                    "name": "DC5521 Input Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.dc5521_input_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_dc5521_input_power",
                })

                # Solar Port 1 (GX16-MF1) input sensors
                self._pub_config("sensor", dk, "gx16mf1_input_voltage", {
                    "name": "Solar Port 1 Voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.gx16mf1_input_voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_gx16mf1_input_voltage",
                })

                self._pub_config("sensor", dk, "gx16mf1_input_current", {
                    "name": "Solar Port 1 Current",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.gx16mf1_input_current }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_gx16mf1_input_current",
                })

                self._pub_config("sensor", dk, "gx16mf1_input_power", {
                    "name": "Solar Port 1 Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.gx16mf1_input_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_gx16mf1_input_power",
                })

                # Solar Port 2 (GX16-MF2) input sensors
                self._pub_config("sensor", dk, "gx16mf2_input_voltage", {
                    "name": "Solar Port 2 Voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.gx16mf2_input_voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_gx16mf2_input_voltage",
                })

                self._pub_config("sensor", dk, "gx16mf2_input_current", {
                    "name": "Solar Port 2 Current",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.gx16mf2_input_current }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_gx16mf2_input_current",
                })

                self._pub_config("sensor", dk, "gx16mf2_input_power", {
                    "name": "Solar Port 2 Power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.gx16mf2_input_power }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_gx16mf2_input_power",
                })

            # Remaining charging time
            self._pub_config("sensor", dk, "remaining_charging_time", {
                "name": "Remaining Charging Time",
                "icon": "mdi:battery-clock",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.remain_charging_hm }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_remaining_charging_time",
            })

            # Total energy (cumulative PV generation)
            self._pub_config("sensor", dk, "total_energy", {
                "name": "Total PV Energy",
                "device_class": "energy",
                "state_class": "total_increasing",
                "unit_of_measurement": "kWh",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.total_energy }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_total_energy",
            })

            # Device status
            self._pub_config("sensor", dk, "device_status", {
                "name": "Device Status",
                "icon": "mdi:battery-sync",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.device_status_hm }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_device_status",
            })

            # Expansion battery pack status
            self._pub_config("binary_sensor", dk, "expansion_pack", {
                "name": "Expansion Pack",
                "device_class": "connectivity",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.add_bat_status_hm }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_expansion_pack",
            })

            # Auto-dim on idle switch
            self._pub_config("switch", dk, "auto_dim", {
                "name": "Auto-Dim on Idle",
                "icon": "mdi:brightness-auto",
                "command_topic": f"pecron/{dk}/auto_light_flag_as/set",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.auto_light_flag_as }}",
                "state_on": "ON", "state_off": "OFF",
                "payload_on": "ON", "payload_off": "OFF",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_auto_dim",
            })

            # Screen brightness level
            self._pub_config("sensor", dk, "screen_brightness", {
                "name": "Screen Brightness",
                "icon": "mdi:brightness-6",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.machine_screen_light_as }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_screen_brightness",
            })

            # AC output voltage setting
            self._pub_config("sensor", dk, "ac_voltage_setting", {
                "name": "AC Output Voltage Setting",
                "icon": "mdi:flash",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.ac_output_voltage_io }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_ac_voltage_setting",
            })

            # AC output frequency setting
            self._pub_config("sensor", dk, "ac_frequency_setting", {
                "name": "AC Output Frequency Setting",
                "icon": "mdi:sine-wave",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.ac_output_frequency_io }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_ac_frequency_setting",
            })

            # No-output auto-off timer
            self._pub_config("sensor", dk, "auto_off_timer", {
                "name": "No-Output Auto-Off Timer",
                "icon": "mdi:timer-off",
                "state_topic": f"pecron/{dk}/state",
                "value_template": "{{ value_json.noastime_io }}",
                "device": dev_info,
                "unique_id": f"pecron_{dk}_auto_off_timer",
            })

            # === WB12200 battery management sensors ===
            if is_wb:
                self._pub_config("sensor", dk, "charging_limit_voltage", {
                    "name": "Charging Limit Voltage",
                    "device_class": "voltage",
                    "icon": "mdi:battery-charging-high",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.charging_limit_voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_charging_limit_voltage",
                })

                self._pub_config("sensor", dk, "discharge_limit_voltage", {
                    "name": "Discharge Limit Voltage",
                    "device_class": "voltage",
                    "icon": "mdi:battery-low",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.discharge_limiting_voltage }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_discharge_limit_voltage",
                })

                self._pub_config("sensor", dk, "charging_current_limit", {
                    "name": "Charging Current Limit",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "icon": "mdi:current-dc",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.charging_current_limit }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_charging_current_limit",
                })

                self._pub_config("sensor", dk, "discharge_current_limit", {
                    "name": "Discharge Current Limit",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "icon": "mdi:current-dc",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.discharge_limiting_current }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_discharge_current_limit",
                })

                self._pub_config("sensor", dk, "battery_heating", {
                    "name": "Battery Heating Mode",
                    "icon": "mdi:radiator",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.battery_heating_mode }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_battery_heating",
                })

                # Beep/voice switch (WB12200)
                self._pub_config("switch", dk, "beep", {
                    "name": "Beep/Voice Alert",
                    "icon": "mdi:volume-high",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.beep_voice_us }}",
                    "state_on": "ON", "state_off": "OFF",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_beep",
                })

                # Fault alarm sensor
                self._pub_config("sensor", dk, "fault_alarm", {
                    "name": "Fault Alarm",
                    "icon": "mdi:alert-circle",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": "{{ value_json.FAULT_ALARM_ENUM }}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_fault_alarm",
                })

            # Per-pack sensors (charging_pack_data_jdb) — packs 0-3 (PPS only)
            if is_pps:
              for pack_num in range(4):
                # Pack battery percentage
                self._pub_config("sensor", dk, f"pack_{pack_num}_battery", {
                    "name": f"Pack {pack_num} Battery",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.pack_{pack_num}_battery }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_pack_{pack_num}_battery",
                })

                # Pack voltage
                self._pub_config("sensor", dk, f"pack_{pack_num}_voltage", {
                    "name": f"Pack {pack_num} Voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.pack_{pack_num}_voltage }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_pack_{pack_num}_voltage",
                })

                # Pack current
                self._pub_config("sensor", dk, f"pack_{pack_num}_current", {
                    "name": f"Pack {pack_num} Current",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.pack_{pack_num}_current }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_pack_{pack_num}_current",
                })

                # Pack temperature
                self._pub_config("sensor", dk, f"pack_{pack_num}_temp", {
                    "name": f"Pack {pack_num} Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.pack_{pack_num}_temp }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_pack_{pack_num}_temp",
                })

                # Pack status
                self._pub_config("sensor", dk, f"pack_{pack_num}_status", {
                    "name": f"Pack {pack_num} Status",
                    "icon": "mdi:battery-sync",
                    "state_topic": f"pecron/{dk}/state",
                    "value_template": f"{{{{ value_json.pack_{pack_num}_status }}}}",
                    "device": dev_info,
                    "unique_id": f"pecron_{dk}_pack_{pack_num}_status",
                })

            # Clear stale entities from previous versions that no longer apply to this model
            self._clear_stale_entities(dk)

        log.info("Published Home Assistant discovery configs")

    def _pub_config(self, component: str, dk: str, key: str, config: dict):
        topic = f"{self.discovery_prefix}/{component}/pecron_{dk}/{key}/config"
        self.client.publish(topic, json.dumps(config), qos=1, retain=True)
        self._published_topics.add(topic)

    def _clear_stale_entities(self, dk: str):
        """Publish empty retained messages for entities that were previously published
        but are no longer relevant (e.g., WB12200-only entities on an E3800).
        This removes stale entities from Home Assistant."""
        # All possible entity keys across all models
        ALL_ENTITY_KEYS = {
            "sensor": [
                "battery", "host_battery", "voltage", "temperature", "current",
                "total_input", "total_output", "remaining_time",
                "battery_temp", "charging_plate_temp", "inverter_temp",
                "ac_input", "dc_input", "ac_output", "dc_output",
                "ac_output_voltage", "ac_output_hz", "ac_output_pf",
                "dc5521_input_voltage", "dc5521_input_current", "dc5521_input_power",
                "gx16mf1_input_voltage", "gx16mf1_input_current", "gx16mf1_input_power",
                "gx16mf2_input_voltage", "gx16mf2_input_current", "gx16mf2_input_power",
                "remaining_charging_time", "total_energy", "device_status",
                "ac_charging_power", "ups_charge_threshold", "standby_timeout",
                "screen_brightness", "auto_off_timer",
                "ac_voltage_setting", "ac_frequency_setting",
                "charging_limit_voltage", "discharge_limit_voltage",
                "charging_current_limit", "discharge_current_limit",
                "battery_heating", "fault_alarm",
            ] + [f"pack_{i}_{f}" for i in range(4) for f in ["battery", "voltage", "current", "temp", "status"]],
            "switch": ["ac", "dc", "ups", "eco_mode", "touch_lock", "bypass", "auto_dim", "beep"],
            "binary_sensor": ["expansion_pack"],
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
        present, v = _get_first_present(SENSOR_FIELDS["voltage"])
        if present:
            try:
                cache["voltage"] = round(float(v), 1)
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

        present, v = _get_first_present(SENSOR_FIELDS["total_input_power"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["total_input_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["total_output_power"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["total_output_power"] = int(float(v))
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
        for field in ("eco_quite_mode_as", "device_touch_locking_as", "bypass_enable", "auto_light_flag_as"):
            v = kv.get(field)
            if v is not None:
                cache[field] = "ON" if _truthy(v) else "OFF"

        for field in ("ac_charging_power_ios", "ups_start_charge_value_as",
                       "device_standy_times_as", "machine_screen_light_as",
                       "ac_output_voltage_io", "ac_output_frequency_io", "noastime_io"):
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

        # Current (amps)
        present, v = _get_first_present(SENSOR_FIELDS["current"])
        if present:
            try:
                cache["current"] = round(float(v), 2)
            except (TypeError, ValueError):
                pass

        # ---- Per-port DC input sensors (solar + barrel) ----
        for field, rounding in [
            ("dc5521_input_voltage", 1), ("dc5521_input_current", 2), ("dc5521_input_power", 0),
            ("gx16mf1_input_voltage", 1), ("gx16mf1_input_current", 2), ("gx16mf1_input_power", 0),
            ("gx16mf2_input_voltage", 1), ("gx16mf2_input_current", 2), ("gx16mf2_input_power", 0),
        ]:
            present, v = _get_first_present(SENSOR_FIELDS[field])
            if present:
                try:
                    cache[field] = round(float(v), rounding) if rounding else int(float(v))
                except (TypeError, ValueError):
                    pass

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
        packs = kv.get("charging_pack_data_jdb", [])
        if isinstance(packs, list):
            for i, pack in enumerate(packs[:4]):
                if not isinstance(pack, dict):
                    continue
                try:
                    status_val = int(float(pack.get("charging_pack_status", 4)))
                except (TypeError, ValueError):
                    status_val = 4
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
                    cache[f"pack_{i}_voltage"] = round(float(pack.get("charging_pack_voltage", 0)), 1)
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_current"] = round(float(pack.get("charging_pack_current", 0)), 2)
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_temp"] = int(float(pack.get("charging_pack_temp", 0)))
                except (TypeError, ValueError):
                    pass

        # ---- SOC vs Host % ----
        # Your device alternates two payload shapes:
        #   * host packet (has host_packet_data_jdb.*) -> host %
        #   * overall packet (no host_packet_data_jdb) -> overall SOC %
        #
        # IMPORTANT: when host_packet_data_jdb is present, battery_percentage mirrors host %,
        # so we *must not* treat it as SOC in that shape.
        if packet_has_host:
            present, v = _get_first_present([("host_packet_data_jdb", "host_packet_electric_percentage")])
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
