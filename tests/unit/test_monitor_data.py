"""Contracts for shared monitor telemetry helpers."""

import pytest

from monitor_data import coerce_switch, extract_power, extract_soc, extract_voltage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("ON", True),
        ("true", True),
        ("1", True),
        ("OFF", False),
        ("unexpected", False),
    ],
)
def test_coerce_switch_preserves_existing_semantics(raw, expected):
    assert coerce_switch(raw) is expected


def test_extract_soc_prefers_host_then_falls_back_to_top_level():
    assert (
        extract_soc(
            {
                "host_packet_data_jdb": {"host_packet_electric_percentage": "42.9"},
                "battery_percentage": 90,
            }
        )
        == 42
    )
    assert (
        extract_soc(
            {
                "host_packet_data_jdb": {"host_packet_electric_percentage": "invalid"},
                "battery_percentage": "75.8",
            }
        )
        == 75
    )
    assert extract_soc({}) is None


def test_extract_voltage_uses_positive_direct_then_sensor_fallback():
    assert extract_voltage({"voltage": "53.25"}) == pytest.approx(53.25)
    assert extract_voltage(
        {
            "voltage": 0,
            "host_packet_data_jdb": {"host_packet_voltage": 52.8},
        }
    ) == pytest.approx(52.8)
    assert extract_voltage({"voltage": "invalid"}) is None


def test_extract_power_prefers_nonzero_total():
    kv = {
        "total_output_power": 900,
        "ac_data_output_hm": {"ac_output_power": 100},
        "dc_data_output_hm": {"dc_output_power": 25},
    }
    assert extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power") == 900


def test_extract_power_uses_complete_components_for_zero_or_missing_total():
    kv = {
        "total_output_power": 0,
        "ac_data_output_hm": {"ac_output_power": "100"},
        "dc_data_output_hm": {"dc_output_power": 25},
    }
    assert extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power") == 125
    del kv["total_output_power"]
    assert extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power") == 125


def test_extract_power_distinguishes_genuine_zero_from_missing():
    assert (
        extract_power(
            {"total_output_power": 0},
            "total_output_power",
            "ac_output_power",
            "dc_output_power",
        )
        == 0
    )
    assert extract_power({}, "total_output_power", "ac_output_power", "dc_output_power") is None
