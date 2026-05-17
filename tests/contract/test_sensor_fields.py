"""
Contract tests for SENSOR_FIELDS — the mapping from logical sensor name to
a list of device-JSON paths.

These tests exist to catch silent schema drift. When Pecron changes a payload
shape or we modularize the code, these tests scream if a sensor field no longer
resolves against a known-good payload.

WORKFLOW for adding real fixtures:
    1. Run `python3 pecron_monitor.py --raw` against your device
    2. Save the "RAW JSON DATA" block to tests/fixtures/mqtt_payloads/<model>.json
       (wrap it in a single top-level object if needed)
    3. Register it in REAL_CAPTURES below
    4. Tests run automatically on the next pytest invocation
"""

import json
import pytest

from constants import SENSOR_FIELDS
from helpers import _get_kv


# ---------------------------------------------------------------------------
# Sensor groups — which fields we expect every Pecron model to expose
# ---------------------------------------------------------------------------

# These *must* be present on any working Pecron telemetry payload.
REQUIRED_SENSORS = {"battery_percent"}

# These are commonly present but model-dependent. We don't fail if missing,
# but we report coverage in a dedicated test.
COMMON_SENSORS = {
    "voltage",
    "temperature",
    "total_input_power",
    "total_output_power",
    "ac_switch",
    "dc_switch",
}

# Model-specific sensors — never expected to be universal.
MODEL_SPECIFIC = {
    "battery_temp",
    "charging_plate_temp",
    "inverter_temp",
    "device_status_hm",
    "add_bat_status_hm",
    "battery_heating_mode",
    "charging_limit_voltage",
    "discharge_limiting_voltage",
    "charging_current_limit",
    "discharge_limiting_current",
}


# ---------------------------------------------------------------------------
# Real device captures — populate these as you gather them
# ---------------------------------------------------------------------------

# Keyed by a human-readable label. Paths are relative to tests/fixtures/mqtt_payloads/.
REAL_CAPTURES: dict[str, str] = {
    # "E1500LFP": "E1500LFP.json",
    # "E300LFP": "E300LFP.json",
    # "E3800LFP": "E3800LFP.json",
    # "WB12200": "WB12200.json",
}


def _load_capture(mqtt_payloads_dir, filename: str) -> dict:
    """Load a captured JSON payload. The file may contain either a raw kv dict
    or a dict wrapping the kv dict under the device_key."""
    path = mqtt_payloads_dir / filename
    data = json.loads(path.read_text())
    # If it's a wrapper like {"AABBCCDDEEFF": {...}}, unwrap to the first value
    if len(data) == 1 and isinstance(next(iter(data.values())), dict):
        return next(iter(data.values()))
    return data


# ---------------------------------------------------------------------------
# Structural tests — verify SENSOR_FIELDS is well-formed regardless of data
# ---------------------------------------------------------------------------


class TestSensorFieldsStructure:
    def test_every_entry_is_a_list(self):
        for sensor, paths in SENSOR_FIELDS.items():
            assert isinstance(paths, list), (
                f"SENSOR_FIELDS[{sensor!r}] must be a list of tuples, got {type(paths).__name__}"
            )

    def test_every_path_is_a_tuple(self):
        for sensor, paths in SENSOR_FIELDS.items():
            for i, path in enumerate(paths):
                assert isinstance(path, tuple), (
                    f"SENSOR_FIELDS[{sensor!r}][{i}] must be a tuple, got {type(path).__name__}"
                )
                assert len(path) > 0, f"SENSOR_FIELDS[{sensor!r}][{i}] is empty"
                for key in path:
                    assert isinstance(key, str), (
                        f"SENSOR_FIELDS[{sensor!r}][{i}] contains non-string key: {key!r}"
                    )

    def test_required_sensors_present(self):
        for s in REQUIRED_SENSORS:
            assert s in SENSOR_FIELDS, f"Required sensor {s!r} missing from SENSOR_FIELDS"

    def test_no_duplicate_paths_within_a_sensor(self):
        for sensor, paths in SENSOR_FIELDS.items():
            assert len(paths) == len(set(paths)), (
                f"SENSOR_FIELDS[{sensor!r}] has duplicate paths: {paths}"
            )


# ---------------------------------------------------------------------------
# Synthetic payload tests — prove the fixture shapes in conftest.py parse
# ---------------------------------------------------------------------------


class TestSyntheticPayloadShapes:
    """These ride on the synthetic fixtures in conftest.py. They verify that
    SENSOR_FIELDS correctly handles BOTH payload shapes we've seen in the wild
    (nested host_packet_data_jdb vs. top-level)."""

    def test_e1500_nested_shape_resolves_battery(self, sample_e1500_telemetry):
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["battery_percent"]) == 73

    def test_e1500_nested_shape_resolves_voltage(self, sample_e1500_telemetry):
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["voltage"]) == 51.8

    def test_e1500_nested_shape_resolves_temperature(self, sample_e1500_telemetry):
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["temperature"]) == 24

    def test_e1500_ac_switch_nested_fallback(self, sample_e1500_telemetry):
        # E1500 doesn't have top-level ac_switch_hm; must fall through to nested path
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["ac_switch"]) == 0

    def test_e1500_dc_switch_nested_fallback(self, sample_e1500_telemetry):
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["dc_switch"]) == 1

    def test_e1500_nested_output_powers(self, sample_e1500_telemetry):
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["ac_output_power"]) == 0
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["dc_output_power"]) == 0
        assert _get_kv(sample_e1500_telemetry, SENSOR_FIELDS["ac_output_voltage"]) == 120

    def test_e300_top_level_shape_resolves_battery(self, sample_e300_telemetry):
        # E300 reports at top level via the fallback path
        assert _get_kv(sample_e300_telemetry, SENSOR_FIELDS["battery_percent"]) == 88

    def test_e300_top_level_shape_resolves_temperature(self, sample_e300_telemetry):
        # E300 uses battery_temp directly (no nested jdb)
        assert _get_kv(sample_e300_telemetry, SENSOR_FIELDS["temperature"]) == 22

    def test_e300_top_level_ac_switch(self, sample_e300_telemetry):
        assert _get_kv(sample_e300_telemetry, SENSOR_FIELDS["ac_switch"]) == 1


# ---------------------------------------------------------------------------
# Real capture tests — only run when fixtures are present
# ---------------------------------------------------------------------------


def _real_capture_ids():
    return list(REAL_CAPTURES.keys())


@pytest.mark.skipif(not REAL_CAPTURES, reason="No real captures registered in REAL_CAPTURES")
@pytest.mark.parametrize("model", _real_capture_ids())
class TestRealCaptures:
    def test_required_sensors_resolve(self, model, mqtt_payloads_dir):
        kv = _load_capture(mqtt_payloads_dir, REAL_CAPTURES[model])
        for sensor in REQUIRED_SENSORS:
            value = _get_kv(kv, SENSOR_FIELDS[sensor])
            assert value is not None, (
                f"{model}: required sensor {sensor!r} failed to resolve. "
                f"Top-level keys in capture: {list(kv.keys())}"
            )

    def test_battery_percent_in_range(self, model, mqtt_payloads_dir):
        kv = _load_capture(mqtt_payloads_dir, REAL_CAPTURES[model])
        bp = _get_kv(kv, SENSOR_FIELDS["battery_percent"])
        assert bp is not None
        assert 0 <= bp <= 100, f"{model}: battery_percent={bp} out of range"

    def test_voltage_sane_if_present(self, model, mqtt_payloads_dir):
        kv = _load_capture(mqtt_payloads_dir, REAL_CAPTURES[model])
        v = _get_kv(kv, SENSOR_FIELDS["voltage"])
        if v is not None:
            # Pecron batteries span roughly 10V (12V systems) to 60V (48V systems)
            assert 5 <= float(v) <= 80, f"{model}: voltage={v} looks wrong"

    def test_temperature_sane_if_present(self, model, mqtt_payloads_dir):
        kv = _load_capture(mqtt_payloads_dir, REAL_CAPTURES[model])
        t = _get_kv(kv, SENSOR_FIELDS["temperature"])
        if t is not None:
            # Operating temps; allow some slack for shipping/cold-climate use
            assert -20 <= float(t) <= 80, f"{model}: temperature={t} looks wrong"


# ---------------------------------------------------------------------------
# Coverage reporting — informational, never fails
# ---------------------------------------------------------------------------


def test_sensor_field_coverage_report(mqtt_payloads_dir, capsys):
    """Prints which sensors resolve against which real captures. Always passes;
    it's a diagnostic aid."""
    if not REAL_CAPTURES:
        pytest.skip("No real captures registered")

    lines = ["", "SENSOR_FIELDS coverage across real captures:", ""]
    header = f"  {'sensor':<28} " + " ".join(f"{m:<12}" for m in REAL_CAPTURES)
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    captures = {m: _load_capture(mqtt_payloads_dir, f) for m, f in REAL_CAPTURES.items()}
    for sensor in sorted(SENSOR_FIELDS):
        row = f"  {sensor:<28} "
        for model, kv in captures.items():
            value = _get_kv(kv, SENSOR_FIELDS[sensor])
            mark = "ok" if value is not None else "--"
            row += f"{mark:<12} "
        lines.append(row)

    with capsys.disabled():
        print("\n".join(lines))
