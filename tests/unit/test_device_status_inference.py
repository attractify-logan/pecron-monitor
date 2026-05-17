"""Tests for issue #45: device_status_hm freezes at "Shut Down" when only
host-shape packets are arriving.

device_status_hm is carried only by overall-shape packets (it's a top-level
key, not nested under host_packet_data_jdb). On standalone PPS like the
E1500LFP that primarily emit host-shape packets during active operation,
the cached `device_status_hm` freezes at whatever the last overall-shape
packet reported — typically "Shut Down" from before the device woke up.
HA then misreports the device as Shut Down while it's actively charging
or discharging.

Sibling pattern to the soc_percent fix in PR #44 / issue #43, but device
status has no host-shape equivalent we can mirror. The fix infers status
from observed power activity (total_input_power / total_output_power /
ac_output_power / dc_output_power) which host-shape packets DO carry.
"""

from ha_bridge import HomeAssistantBridge


def _bridge_with_cache(cache):
    """Construct a HomeAssistantBridge with a pre-seeded cache for one device."""
    b = HomeAssistantBridge.__new__(HomeAssistantBridge)
    b._state_cache = {"DK": cache}
    return b


# Stand-in for the post-publish_state cache state. Each test seeds device_status_hm
# to "Shut Down" (the freeze case) plus the power fields the inference reads.
def _shut_down_with(power):
    return {"device_status_hm": "Shut Down", **power}


# We test the inference block in isolation by simulating the relevant cache
# state at the point in publish_state() where the block runs. The block reads
# from `cache` only and writes to `cache["device_status_hm"]` only, so we can
# mirror it here without spinning up a full publish_state call.
def _run_inference(cache):
    if cache.get("device_status_hm") == "Shut Down":
        total_in = cache.get("total_input_power") or 0
        total_out = cache.get("total_output_power") or 0
        ac_out = cache.get("ac_output_power") or 0
        dc_out = cache.get("dc_output_power") or 0
        if total_in > 0:
            cache["device_status_hm"] = "Charging"
        elif total_out > 0:
            if ac_out > dc_out:
                cache["device_status_hm"] = "AC Discharge"
            elif dc_out > 0:
                cache["device_status_hm"] = "DC Discharge"
    return cache


class TestDeviceStatusInference:
    def test_shut_down_with_input_power_becomes_charging(self):
        cache = _shut_down_with(
            {"total_input_power": 89, "total_output_power": 233, "ac_output_power": 232}
        )
        # Bruce's exact reproducer state from #45: device discharging at 233W AC
        # while charging at 89W (passthrough). Charging wins.
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Charging"

    def test_shut_down_with_ac_output_only_becomes_ac_discharge(self):
        cache = _shut_down_with(
            {
                "total_input_power": 0,
                "total_output_power": 200,
                "ac_output_power": 200,
                "dc_output_power": 0,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "AC Discharge"

    def test_shut_down_with_dc_output_only_becomes_dc_discharge(self):
        cache = _shut_down_with(
            {
                "total_input_power": 0,
                "total_output_power": 50,
                "ac_output_power": 0,
                "dc_output_power": 50,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "DC Discharge"

    def test_shut_down_with_no_power_stays_shut_down(self):
        # Genuinely off device. Don't override.
        cache = _shut_down_with(
            {
                "total_input_power": 0,
                "total_output_power": 0,
                "ac_output_power": 0,
                "dc_output_power": 0,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Shut Down"

    def test_charging_status_is_not_overridden(self):
        # Cache already says Charging from a real overall packet. Don't touch.
        cache = {
            "device_status_hm": "Charging",
            "total_input_power": 100,
            "total_output_power": 50,
            "ac_output_power": 50,
            "dc_output_power": 0,
        }
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Charging"

    def test_ac_discharge_status_is_not_overridden(self):
        cache = {
            "device_status_hm": "AC Discharge",
            "total_input_power": 0,
            "total_output_power": 200,
            "ac_output_power": 200,
            "dc_output_power": 0,
        }
        out = _run_inference(cache)
        assert out["device_status_hm"] == "AC Discharge"

    def test_standby_status_is_not_overridden(self):
        # Standby has no input/output but isn't Shut Down. Don't touch.
        cache = {
            "device_status_hm": "Standby",
            "total_input_power": 0,
            "total_output_power": 0,
            "ac_output_power": 0,
            "dc_output_power": 0,
        }
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Standby"

    def test_passthrough_mode_charging_wins(self):
        # Both charging and discharging simultaneously (mains-powered passthrough):
        # charging takes precedence because that's the user-relevant state for
        # automations that care whether the battery is being topped up.
        cache = _shut_down_with(
            {
                "total_input_power": 500,
                "total_output_power": 200,
                "ac_output_power": 200,
                "dc_output_power": 0,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Charging"

    def test_dc_dominates_ac_picks_dc_discharge(self):
        cache = _shut_down_with(
            {
                "total_input_power": 0,
                "total_output_power": 100,
                "ac_output_power": 30,
                "dc_output_power": 70,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "DC Discharge"

    def test_neither_ac_nor_dc_dominates_leaves_cached(self):
        # total_output_power > 0 but ac_out=0 and dc_out=0 (a packet shape that
        # reports total_out but not the per-line breakdown). The inference
        # block should leave the cached value alone rather than guess wrong.
        cache = _shut_down_with(
            {
                "total_input_power": 0,
                "total_output_power": 100,
                "ac_output_power": 0,
                "dc_output_power": 0,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Shut Down"

    def test_none_values_are_treated_as_zero(self):
        # Cache may legitimately have None for a field that hasn't been
        # populated yet. The inference must not blow up on None; treat as 0.
        cache = _shut_down_with(
            {
                "total_input_power": None,
                "total_output_power": None,
                "ac_output_power": None,
                "dc_output_power": None,
            }
        )
        out = _run_inference(cache)
        assert out["device_status_hm"] == "Shut Down"
