"""Pure telemetry helpers shared by monitor policy components."""

from constants import SENSOR_FIELDS
from helpers import _get_kv


def coerce_switch(value):
    """Coerce an observed switch value to bool, preserving missing values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.upper() in ("ON", "TRUE", "1")
    return bool(value)


def extract_soc(kv: dict):
    """Extract battery state of charge using the canonical field precedence."""
    host = kv.get("host_packet_data_jdb")
    if isinstance(host, dict):
        value = host.get("host_packet_electric_percentage")
        if value is not None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                pass
    value = kv.get("battery_percentage")
    if value is not None:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            pass
    return None


def extract_voltage(kv: dict):
    """Extract a positive battery voltage, or return None when unavailable."""
    for value in (kv.get("voltage"), _get_kv(kv, SENSOR_FIELDS["voltage"], 0)):
        try:
            voltage = float(value)
        except (TypeError, ValueError):
            continue
        if voltage > 0:
            return voltage
    return None


def extract_power(kv: dict, total_key: str, ac_key: str, dc_key: str):
    """Resolve total power with the monitor's existing AC+DC fallback semantics."""

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    total = as_int(_get_kv(kv, SENSOR_FIELDS[total_key]))
    ac = _get_kv(kv, SENSOR_FIELDS[ac_key])
    dc = _get_kv(kv, SENSOR_FIELDS[dc_key])
    components = None
    if ac is not None and dc is not None:
        ac_value, dc_value = as_int(ac), as_int(dc)
        if ac_value is not None and dc_value is not None:
            components = ac_value + dc_value
    if total is not None and total != 0:
        return total
    if components is not None:
        return components
    return total
