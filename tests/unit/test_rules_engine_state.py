"""Tests for rules engine state, init, and external commands (#56)."""

import json
import sys
from unittest.mock import MagicMock

from monitor import PecronMonitor


def _make_monitor(tmp_path, rules, *, initial_state="normal"):
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
    monitor.set_dc = MagicMock(return_value=True)
    monitor.set_ups = MagicMock(return_value=True)
    return monitor


def test_rule_state_action_persists_and_new_monitor_loads_it(tmp_path):
    rules = [
        {
            "name": "enter low state",
            "condition": {"state": "normal", "battery_below": 50},
            "action": {"set_state": "low"},
            "cooldown_minutes": 0,
        }
    ]
    monitor = _make_monitor(tmp_path, rules)

    monitor._evaluate_rules("DK", {"battery_percentage": 40, "voltage": 52.0}, 40)

    assert monitor.rule_states["default"] == "low"
    state_file = tmp_path / "rules-state.json"
    assert json.loads(state_file.read_text())["states"]["default"] == "low"
    reloaded = _make_monitor(tmp_path, [], initial_state="normal")
    assert reloaded.rule_states["default"] == "low"


def test_state_gate_prevents_rule_in_wrong_state(tmp_path):
    monitor = _make_monitor(
        tmp_path,
        [
            {
                "name": "only peak",
                "condition": {"state": "peak", "battery_below": 50},
                "action": {"set_ac": False},
                "cooldown_minutes": 0,
            }
        ],
    )

    monitor._evaluate_rules("DK", {"battery_percentage": 40, "voltage": 52.0}, 40)

    monitor.set_ac.assert_not_called()


def test_states_gate_accepts_any_listed_state(tmp_path):
    monitor = _make_monitor(
        tmp_path,
        [
            {
                "name": "normal or peak",
                "condition": {"states": ["normal", "peak"], "battery_below": 50},
                "action": {"set_dc": False},
                "cooldown_minutes": 0,
            }
        ],
    )

    monitor._evaluate_rules("DK", {"battery_percentage": 40, "voltage": 52.0}, 40)

    monitor.set_dc.assert_called_once_with("DK", False)


def test_init_rule_fires_once(tmp_path):
    monitor = _make_monitor(
        tmp_path,
        [
            {
                "name": "startup ac off",
                "condition": {"init": True},
                "action": {"set_ac": False},
                "cooldown_minutes": 0,
            }
        ],
    )

    monitor._run_init_rules()
    monitor._run_init_rules()

    monitor.set_ac.assert_called_once_with("DK", False)


def test_run_command_receives_rule_context_json(tmp_path):
    script = tmp_path / "capture_rule_payload.py"
    output = tmp_path / "payload.json"
    script.write_text(
        "import json, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True))\n"
    )
    monitor = _make_monitor(
        tmp_path,
        [
            {
                "name": "external command",
                "condition": {"battery_below": 50},
                "action": {
                    "run_command": [sys.executable, str(script), str(output)],
                    "timeout_seconds": 5,
                },
                "cooldown_minutes": 0,
            }
        ],
    )

    monitor._evaluate_rules("DK", {"battery_percentage": 40, "voltage": 51.2}, 40)

    payload = json.loads(output.read_text())
    assert payload["rule"] == "external command"
    assert payload["state"] == "normal"
    assert payload["device_key"] == "DK"
    assert payload["target_device_key"] == "DK"
    assert payload["battery_percent"] == 40
    assert payload["voltage"] == 51.2
    assert payload["data"]["battery_percentage"] == 40
