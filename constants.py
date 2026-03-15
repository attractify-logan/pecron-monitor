"""
Configuration constants for pecron-monitor.

Contains region configurations, known product mappings, default control definitions,
and sensor field mappings used across all Pecron device models.
"""

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
# Known product name mappings
# ---------------------------------------------------------------------------
KNOWN_PRODUCTS = {
    "E1500LFP": "E1500 LFP",
    "E300LFP": "E300 LFP",
    "E600LFP": "E600 LFP",
    "E2000LFP": "E2000 LFP",
    "E3000LFP": "E3000 LFP",
}

# ---------------------------------------------------------------------------
# Data point IDs (from Quectel TSL — Thing Specification Language)
# These are universal for each Pecron product model.
# The TSL is fetched dynamically; these are E1500LFP defaults as fallback.
# ---------------------------------------------------------------------------
DEFAULT_CONTROLS = {
    # Common controls (E1500, E2000, E3000, WB12200)
    "ac_switch_hm":           {"id": 40, "type": "BOOL", "desc": "AC output", "access": "RW"},
    "dc_switch_hm":           {"id": 38, "type": "BOOL", "desc": "DC output", "access": "RW"},
    "ups_status_hm":          {"id": 27, "type": "BOOL", "desc": "UPS mode", "access": "RW"},
    "auto_light_flag_as":     {"id": 43, "type": "BOOL", "desc": "Auto screen light", "access": "RW"},
    "machine_screen_light_as":{"id": 45, "type": "ENUM", "desc": "Screen brightness", "access": "RW"},
    # E3800/E3600-specific controls
    "eco_quite_mode_as":      {"id": 44, "type": "BOOL", "desc": "Eco/quiet mode", "access": "RW"},
    "ups_start_charge_value_as": {"id": 46, "type": "INT", "desc": "UPS charge threshold %", "access": "RW"},
    "ac_charging_power_ios":  {"id": 50, "type": "INT", "desc": "AC charging power", "access": "RW"},
    "device_touch_locking_as":{"id": 42, "type": "BOOL", "desc": "Touch panel lock", "access": "RW"},
    "device_standy_times_as": {"id": 51, "type": "INT", "desc": "Standby timeout", "access": "RW"},
    # WB12200-specific controls
    "battery_heating_mode":   {"id": 91, "type": "INT", "desc": "Battery heating mode", "access": "RW"},
    "charging_limit_voltage": {"id": 92, "type": "INT", "desc": "Charging limit voltage", "access": "RW"},
    "discharge_limiting_voltage": {"id": 93, "type": "INT", "desc": "Discharge limit voltage", "access": "RW"},
    "charging_current_limit": {"id": 94, "type": "INT", "desc": "Charging current limit", "access": "RW"},
    "discharge_limiting_current": {"id": 95, "type": "INT", "desc": "Discharge current limit", "access": "RW"},
}

# ---------------------------------------------------------------------------
# Common sensor field mappings — works across all known Pecron models.
# Each sensor maps to a list of paths to try (first match wins).
# Some models (E1500LFP) nest battery/voltage in host_packet_data_jdb,
# while others (E300LFP) report them at the top level.
# ---------------------------------------------------------------------------
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
        ("battery_temp",),  # E3800 uses separate temp sensors
    ],
    # E3800-specific temperature sensors (3 separate sensors)
    "battery_temp": [("battery_temp",)],
    "charging_plate_temp": [("charging_plate_temp",)],
    "inverter_temp": [("inverter_temp",)],
    "charge_status": [
        ("host_packet_data_jdb", "host_packet_status"),
    ],
    "total_input_power": [("total_input_power",)],
    "total_output_power": [("total_output_power",)],
    "remain_time": [("remain_time",)],
    "remain_charging_time": [("remain_charging_time",)],
    "ac_output_power": [("ac_data_output_hm", "ac_output_power")],
    "ac_output_voltage": [("ac_data_output_hm", "ac_output_voltage")],
    "dc_output_power": [("dc_data_output_hm", "dc_output_power")],
    "ac_input_power": [("ac_data_input_hm", "ac_power")],
    "dc_input_power": [("dc_data_input_hm", "dc_input_power")],
    # Per-port DC input (solar ports + barrel)
    "dc5521_input_voltage": [("dc_data_input_hm", "dc5521_input_voltage")],
    "dc5521_input_current": [("dc_data_input_hm", "dc5521_input_current")],
    "dc5521_input_power": [("dc_data_input_hm", "dc5521_input_power")],
    "gx16mf1_input_voltage": [("dc_data_input_hm", "gx16mf1_input_voltage")],
    "gx16mf1_input_current": [("dc_data_input_hm", "gx16mf1_input_current")],
    "gx16mf1_input_power": [("dc_data_input_hm", "gx16mf1_input_power")],
    "gx16mf2_input_voltage": [("dc_data_input_hm", "gx16mf2_input_voltage")],
    "gx16mf2_input_current": [("dc_data_input_hm", "gx16mf2_input_current")],
    "gx16mf2_input_power": [("dc_data_input_hm", "gx16mf2_input_power")],
    # AC output frequency and power factor (actual readings, not settings)
    "ac_output_hz": [("ac_data_output_hm", "ac_output_hz")],
    "ac_output_pf": [("ac_data_output_hm", "ac_output_pf")],
    "ac_switch": [("ac_switch_hm",), ("host_packet_data_jdb","host_packet_ac_switch"), ("host_packet_data_jdb","ac_switch")],
    "dc_switch": [("dc_switch_hm",), ("host_packet_data_jdb","host_packet_dc_switch"), ("host_packet_data_jdb","dc_switch")],
    "ups_mode": [("ups_status_hm",), ("host_packet_data_jdb","host_packet_ups_status"), ("host_packet_data_jdb","ups_status")],
    # E3800-specific fields
    "device_status_hm": [("device_status_hm",)],
    "add_bat_status_hm": [("add_bat_status_hm",)],
    # WB12200-specific battery management fields
    "battery_heating_mode": [("battery_heating_mode",)],
    "charging_limit_voltage": [("charging_limit_voltage",)],
    "discharge_limiting_voltage": [("discharge_limiting_voltage",)],
    "charging_current_limit": [("charging_current_limit",)],
    "discharge_limiting_current": [("discharge_limiting_current",)],
    # Current (amps) — useful for motorhome/RV monitoring
    "current": [
        ("host_packet_data_jdb", "host_packet_current"),
    ],
    # Per-port DC input (solar ports + barrel jack)
    "dc5521_input_voltage": [("dc_data_input_hm", "dc5521_input_voltage")],
    "dc5521_input_current": [("dc_data_input_hm", "dc5521_input_current")],
    "dc5521_input_power": [("dc_data_input_hm", "dc5521_input_power")],
    "gx16mf1_input_voltage": [("dc_data_input_hm", "gx16mf1_input_voltage")],
    "gx16mf1_input_current": [("dc_data_input_hm", "gx16mf1_input_current")],
    "gx16mf1_input_power": [("dc_data_input_hm", "gx16mf1_input_power")],
    "gx16mf2_input_voltage": [("dc_data_input_hm", "gx16mf2_input_voltage")],
    "gx16mf2_input_current": [("dc_data_input_hm", "gx16mf2_input_current")],
    "gx16mf2_input_power": [("dc_data_input_hm", "gx16mf2_input_power")],
    # AC output actual readings (not settings)
    "ac_output_hz": [("ac_data_output_hm", "ac_output_hz")],
    "ac_output_pf": [("ac_data_output_hm", "ac_output_pf")],
}

# ---------------------------------------------------------------------------
# Enum decode tables — maps raw enum index to human-readable label
# ---------------------------------------------------------------------------

PACK_STATUS_LABELS = {
    0: "No Charge",
    1: "Cascade Charging",
    2: "Balance No Charge",
    3: "Balanced Charging",
    4: "No Connection",
}

DEVICE_STATUS_LABELS = {
    0: "Shut Down",
    1: "Charging",
    2: "DC Discharge",
    3: "AC Discharge",
    4: "Standby",
    5: "Conservation",
}

PACK_STATUS_LABELS = {
    0: "No Charge",
    1: "Cascade Charging",
    2: "Balance No Charge",
    3: "Balanced Charging",
    4: "No Connection",
}

# WB12200 charge limit voltage (enum index → actual voltage)
WB_CHARGE_VOLTAGE_LABELS = {
    0: "12.8V", 1: "13.2V", 2: "13.6V", 3: "14.0V", 4: "14.4V", 5: "14.6V",
}

# WB12200 discharge limit voltage
WB_DISCHARGE_VOLTAGE_LABELS = {
    0: "10.4V", 1: "10.8V", 2: "11.2V", 3: "11.6V", 4: "12.0V",
}

# WB12200 charge current limit
WB_CHARGE_CURRENT_LABELS = {
    0: "50A", 1: "60A", 2: "70A", 3: "80A", 4: "90A",
    5: "100A", 6: "110A", 7: "120A",
}

# WB12200 discharge current limit
WB_DISCHARGE_CURRENT_LABELS = {
    0: "80A", 1: "100A", 2: "120A", 3: "140A", 4: "160A",
    5: "180A", 6: "200A", 7: "220A",
}

# WB12200 battery heating mode
WB_HEATING_MODE_LABELS = {
    0: "Charging Heating", 1: "Keep Warm",
}

# WB12200 battery coding (for multi-battery pairing)
WB_BATTERY_CODING_LABELS = {
    1: "1", 2: "2", 3: "3", 4: "4", 5: "5",
    6: "6", 7: "7", 8: "8", 9: "9", 10: "Host",
}

# WB12200 standby time
WB_STANDBY_TIME_LABELS = {
    0: "24 Hours", 1: "48 Hours", 2: "7 Days", 3: "14 Days", 4: "Always On",
}

# Fault alarm codes (WB12200 + general)
FAULT_ALARM_LABELS = {
    0: "Normal",
    1: "Battery Over-Temperature",
    2: "Battery Overvoltage",
    3: "Battery Undervoltage",
    4: "Battery Short Circuit",
    5: "Charge Overcurrent",
    6: "Discharge Overcurrent",
    7: "Charger Input Overvoltage",
    8: "Charger Input Overcurrent",
    9: "Charger Output Overvoltage",
    10: "Charger Output Overcurrent",
    11: "Charger Output Short Circuit",
    12: "Charger Input Undervoltage",
    13: "Charger Overheating",
    14: "DC Output Short Circuit",
    15: "DC Output Overvoltage",
    16: "DC Output Overcurrent",
    17: "DC Output Over-Temperature",
}

# Master enum labels lookup — maps TSL control code → decode table
ENUM_LABELS = {
    "charging_limit_voltage_iaos": WB_CHARGE_VOLTAGE_LABELS,
    "discharge_limiting_voltage_iaos": WB_DISCHARGE_VOLTAGE_LABELS,
    "charging_current_limit_iaos": WB_CHARGE_CURRENT_LABELS,
    "discharge_limiting_current_iaos": WB_DISCHARGE_CURRENT_LABELS,
    "battery_heating_mode_us": WB_HEATING_MODE_LABELS,
    "device_status_hm": DEVICE_STATUS_LABELS,
    "device_standy_times_as": WB_STANDBY_TIME_LABELS,
    "battery_coding_us": WB_BATTERY_CODING_LABELS,
    "FAULT_ALARM_ENUM": FAULT_ALARM_LABELS,
    "high_frequency_reporting": {
        0: "Infrequent", 1: "LAN High-Freq",
        2: "WiFi High-Freq", 3: "LAN+WiFi High-Freq",
    },
    # Common enums (E1500, E3800, etc.)
    "machine_screen_light_as": {
        0: "5%", 1: "20%", 2: "50%", 3: "80%", 4: "100%",
    },
    "noastime_io": {
        0: "Off", 1: "1 Hour", 2: "2 Hours", 3: "3 Hours", 4: "4 Hours",
    },
    "ac_output_voltage_io": {
        0: "100V", 1: "110V", 2: "120V", 3: "220V", 4: "230V", 5: "240V",
    },
    "ac_output_frequency_io": {
        0: "50Hz", 1: "60Hz",
    },
    "ac_charging_power_ios": {
        0: "0%", 1: "10%", 2: "20%", 3: "30%", 4: "40%",
        5: "50%", 6: "60%", 7: "70%", 8: "80%", 9: "90%", 10: "100%",
    },
}
