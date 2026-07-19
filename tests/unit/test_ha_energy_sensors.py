"""Focused tests for opt-in Home Assistant energy sensors (issue #79)."""

import json
from unittest.mock import MagicMock

import pytest

from energy_state import EnergyIntegrator
from ha_bridge import HomeAssistantBridge


class FakeClock:
    def __init__(self, initial=0.0):
        self.now = float(initial)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _device(device_key="DEV1", name="E1500LFP"):
    return {"device_key": device_key, "device_name": name, "controls": {}}


def _bridge(tmp_path, *, enabled, max_gap=7200, devices=None):
    bridge = HomeAssistantBridge(
        {
            "discovery_prefix": "homeassistant",
            "clear_discovery_on_startup": False,
            "energy_sensors": enabled,
            "energy_state_path": str(tmp_path / "energy.json"),
            "energy_max_gap_seconds": max_gap,
        },
        devices=devices or [_device()],
    )
    bridge.client = MagicMock()
    bridge._connected = True
    return bridge


def _power_payload(ac_input, ac_output, dc_output):
    return {
        "ac_data_input_hm": {"ac_input_power": ac_input},
        "ac_data_output_hm": {"ac_output_power": ac_output},
        "dc_data_output_hm": {"dc_output_power": dc_output},
    }


def _published_state(bridge):
    calls = [
        call.args
        for call in bridge.client.publish.call_args_list
        if call.args[0].endswith("/state")
    ]
    return json.loads(calls[-1][1])


def test_energy_discovery_is_opt_in_and_energy_dashboard_compatible(tmp_path):
    expected = {"ac_input_energy", "ac_output_energy", "dc_output_energy"}
    disabled = _bridge(tmp_path, enabled=False)
    disabled._publish_discovery()
    disabled_payloads = [
        call.args[1]
        for call in disabled.client.publish.call_args_list
        if call.args[0].split("/")[-2] in expected and call.args[1]
    ]
    assert disabled_payloads == []
    cleared_keys = {
        call.args[0].split("/")[-2]
        for call in disabled.client.publish.call_args_list
        if call.args[0].split("/")[-2] in expected and call.args[1] == ""
    }
    assert cleared_keys == expected

    enabled = _bridge(tmp_path, enabled=True)
    enabled._publish_discovery()
    payloads = {}
    for call in enabled.client.publish.call_args_list:
        topic, raw_payload = call.args[:2]
        key = topic.split("/")[-2]
        if key in expected and raw_payload:
            payloads[key] = json.loads(raw_payload)

    assert set(payloads) == expected
    for key, payload in payloads.items():
        assert payload["device_class"] == "energy"
        assert payload["state_class"] == "total_increasing"
        assert payload["unit_of_measurement"] == "kWh"
        assert payload["value_template"] == f"{{{{ value_json.{key} }}}}"
    assert "total_energy" not in payloads


def test_energy_discovery_excludes_non_power_station_devices(tmp_path):
    bridge = _bridge(tmp_path, enabled=True, devices=[_device(name="WB12200")])
    bridge._publish_discovery()
    assert not any(
        call.args[0].endswith("_energy/config") and call.args[1]
        for call in bridge.client.publish.call_args_list
    )


def test_bridge_accumulates_each_channel_with_controllable_clock(tmp_path):
    bridge = _bridge(tmp_path, enabled=True)
    clock = FakeClock()
    bridge._energy.clock = clock

    bridge.publish_state("DEV1", _power_payload(1000, 500, 100))
    first = _published_state(bridge)
    assert first["ac_input_energy"] == 0.0
    assert first["ac_output_energy"] == 0.0
    assert first["dc_output_energy"] == 0.0

    clock.advance(3600)
    bridge.publish_state("DEV1", _power_payload(1000, 1000, 300))
    state = _published_state(bridge)
    assert state["ac_input_energy"] == pytest.approx(1.0)
    assert state["ac_output_energy"] == pytest.approx(0.75)
    assert state["dc_output_energy"] == pytest.approx(0.2)


def test_persisted_counters_reload_across_bridge_restart(tmp_path):
    clock = FakeClock()
    first_bridge = _bridge(tmp_path, enabled=True)
    first_bridge._energy.clock = clock
    first_bridge.publish_state("DEV1", _power_payload(600, 300, 120))
    clock.advance(60)
    first_bridge.publish_state("DEV1", _power_payload(600, 300, 120))
    before_restart = _published_state(first_bridge)

    restarted_bridge = _bridge(tmp_path, enabled=True)
    restarted_bridge._energy.clock = FakeClock(10_000)
    restarted_bridge.publish_state("DEV1", _power_payload(600, 300, 120))
    after_restart = _published_state(restarted_bridge)

    assert after_restart["ac_input_energy"] == before_restart["ac_input_energy"]
    assert after_restart["ac_output_energy"] == before_restart["ac_output_energy"]
    assert after_restart["dc_output_energy"] == before_restart["dc_output_energy"]
    assert not list(tmp_path.glob(".pecron-energy-*"))


def test_unavailable_values_break_continuity_without_adding_energy(tmp_path):
    clock = FakeClock()
    integrator = EnergyIntegrator(
        configured_path=str(tmp_path / "energy.json"), max_gap_seconds=300, clock=clock
    )
    integrator.update("DEV", {"ac_input": 1000, "ac_output": 500, "dc_output": 100})

    clock.advance(60)
    assert integrator.update("DEV", {"ac_input": None, "ac_output": "not-a-number"}) == {}
    clock.advance(60)
    resumed = integrator.update("DEV", {"ac_input": 1000, "ac_output": 500, "dc_output": 100})

    assert resumed["ac_input"] == 0.0
    assert resumed["ac_output"] == 0.0
    assert resumed["dc_output"] == pytest.approx(100 * 120 / 3_600_000)


def test_default_gap_allows_e3600_twenty_minute_telemetry_cadence(tmp_path):
    clock = FakeClock()
    integrator = EnergyIntegrator(configured_path=str(tmp_path / "energy.json"), clock=clock)
    integrator.update("E3600", {"ac_input": 1000})

    clock.advance(20 * 60)
    total = integrator.update("E3600", {"ac_input": 1000})["ac_input"]

    assert total == pytest.approx(1 / 3)


def test_non_positive_time_and_excessive_gap_do_not_add_energy(tmp_path):
    clock = FakeClock(100)
    integrator = EnergyIntegrator(
        configured_path=str(tmp_path / "energy.json"), max_gap_seconds=60, clock=clock
    )
    assert integrator.update("DEV", {"ac_input": 1000}) == {"ac_input": 0.0}
    assert integrator.update("DEV", {"ac_input": 2000}) == {"ac_input": 0.0}

    clock.advance(-10)
    assert integrator.update("DEV", {"ac_input": 2000}) == {"ac_input": 0.0}
    clock.advance(71)
    assert integrator.update("DEV", {"ac_input": 2000}) == {"ac_input": 0.0}


def test_devices_accumulate_in_isolation(tmp_path):
    clock = FakeClock()
    integrator = EnergyIntegrator(
        configured_path=str(tmp_path / "energy.json"), max_gap_seconds=300, clock=clock
    )
    integrator.update("A", {"ac_input": 1000})
    integrator.update("B", {"ac_input": 2000})
    clock.advance(180)

    total_a = integrator.update("A", {"ac_input": 1000})["ac_input"]
    total_b = integrator.update("B", {"ac_input": 2000})["ac_input"]

    assert total_a == pytest.approx(0.05)
    assert total_b == pytest.approx(0.1)
    persisted = json.loads((tmp_path / "energy.json").read_text())["devices"]
    assert persisted["A"]["ac_input"] == pytest.approx(0.05)
    assert persisted["B"]["ac_input"] == pytest.approx(0.1)
