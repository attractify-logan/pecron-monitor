"""Tests for multiple named rule-state variables (#56 stretch goal)."""

import json
import sys
from unittest.mock import MagicMock

from monitor import PecronMonitor


def _make_monitor(tmp_path, rules, *, initial_state="default"):
    config = {
        "email": "test@example.com",
        "password": "test",
        "region": "na",
        "devices": [],
        "rule_state": {
            "initial_state": initial_state,
            "path": str(tmp_path / "rules-state.json"),
        },
        "rules": rules,
    }
    monitor = PecronMonitor(config)
    monitor.devices = [
        {
            "device_key": "DK",
            "device_name": "TestDevice",
            "controls": {"ac_switch_hm": {}, "dc_switch_hm": {}, "ups_status_hm": {}},
        }
    ]
    monitor.set_ac = MagicMock(return_value=True)
    return monitor


def _ac_rule(condition=None, action=None):
    return {
        "name": "r",
        "condition": condition or {"battery_below": 50},
        "action": action or {"set_ac": False},
        "cooldown_minutes": 0,
    }


# --- initial state ----------------------------------------------------------


def test_initial_state_dict_sets_named_vars(tmp_path):
    m = _make_monitor(tmp_path, [], initial_state={"mode": "off", "charge": "idle"})
    assert m.rule_states == {"mode": "off", "charge": "idle"}


def test_legacy_string_initial_uses_default_var(tmp_path):
    m = _make_monitor(tmp_path, [], initial_state="normal")
    assert m.rule_states == {"default": "normal"}


# --- state gate (dict: all named vars must match) ---------------------------


def test_state_gate_dict_all_match_fires(tmp_path):
    cond = {"state": {"mode": "peak", "charge": "armed"}, "battery_below": 50}
    m = _make_monitor(tmp_path, [_ac_rule(cond)], initial_state={"mode": "peak", "charge": "armed"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    m.set_ac.assert_called_once_with("DK", False)


def test_state_gate_dict_one_mismatch_blocks(tmp_path):
    cond = {"state": {"mode": "peak", "charge": "armed"}, "battery_below": 50}
    m = _make_monitor(tmp_path, [_ac_rule(cond)], initial_state={"mode": "peak", "charge": "idle"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    m.set_ac.assert_not_called()


# --- states gate (dict: var -> allowed list) --------------------------------


def test_states_gate_dict_member_fires(tmp_path):
    cond = {"states": {"mode": ["peak", "shoulder"]}, "battery_below": 50}
    m = _make_monitor(tmp_path, [_ac_rule(cond)], initial_state={"mode": "shoulder"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    m.set_ac.assert_called_once_with("DK", False)


def test_states_gate_dict_non_member_blocks(tmp_path):
    cond = {"states": {"mode": ["peak", "shoulder"]}, "battery_below": 50}
    m = _make_monitor(tmp_path, [_ac_rule(cond)], initial_state={"mode": "off"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    m.set_ac.assert_not_called()


# --- set_state (dict) -------------------------------------------------------


def test_set_state_dict_persists_and_reloads(tmp_path):
    rule = _ac_rule(action={"set_state": {"mode": "peak", "charge": "armed"}})
    m = _make_monitor(tmp_path, [rule], initial_state={"mode": "off", "charge": "idle"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    assert m.rule_states == {"mode": "peak", "charge": "armed"}
    data = json.loads((tmp_path / "rules-state.json").read_text())
    assert data["states"] == {"mode": "peak", "charge": "armed"}
    reloaded = _make_monitor(tmp_path, [], initial_state={"mode": "off", "charge": "idle"})
    assert reloaded.rule_states == {"mode": "peak", "charge": "armed"}


def test_set_state_named_var_leaves_others_unchanged(tmp_path):
    rule = _ac_rule(action={"set_state": {"mode": "on"}})
    m = _make_monitor(tmp_path, [rule], initial_state={"mode": "off", "charge": "idle"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    assert m.rule_states == {"mode": "on", "charge": "idle"}


def test_legacy_string_set_state_updates_default_var(tmp_path):
    rule = _ac_rule(action={"set_state": "low"})
    m = _make_monitor(tmp_path, [rule], initial_state="normal")
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    assert m.rule_states == {"default": "low"}


# --- persistence migration --------------------------------------------------


def test_legacy_persisted_state_file_is_migrated(tmp_path):
    (tmp_path / "rules-state.json").write_text(json.dumps({"state": "low", "updated_at": "x"}))
    m = _make_monitor(tmp_path, [], initial_state="normal")
    assert m.rule_states == {"default": "low"}


# --- run_command payload ----------------------------------------------------


def test_run_command_payload_includes_states_dict(tmp_path):
    script = tmp_path / "cap.py"
    output = tmp_path / "out.json"
    script.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(json.load(sys.stdin)))\n"
    )
    rule = _ac_rule(
        action={"run_command": [sys.executable, str(script), str(output)], "timeout_seconds": 5}
    )
    m = _make_monitor(tmp_path, [rule], initial_state={"mode": "peak", "charge": "armed"})
    m._evaluate_rules("DK", {"voltage": 52.0}, 40)
    payload = json.loads(output.read_text())
    assert payload["states"] == {"mode": "peak", "charge": "armed"}
    assert payload["state"] is None  # no `default` variable configured
