"""Behavioral boundaries for automation rule evaluation and actions."""

import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from monitor import PecronMonitor


def _config(rules=None, **updates):
    config = {
        "email": "test@example.com",
        "password": "test",
        "region": "na",
        "devices": [],
        "rules": rules or [],
    }
    config.update(updates)
    return config


def _make_monitor(rules=None, *, config=None, controls=None):
    config = config or _config(rules)
    with patch.object(Path, "exists", return_value=False):
        monitor = PecronMonitor(config)
    monitor.devices = [
        {
            "device_key": "DK",
            "device_name": "TestDevice",
            "controls": controls
            if controls is not None
            else {"ac_switch_hm": {}, "dc_switch_hm": {}, "ups_status_hm": {}},
        }
    ]
    monitor.set_ac = MagicMock(return_value=True)
    monitor.set_dc = MagicMock(return_value=True)
    monitor.set_ups = MagicMock(return_value=True)
    monitor._save_rule_states = MagicMock()
    return monitor


def _rule(name, action, *, condition=None, cooldown_minutes=0):
    return {
        "name": name,
        "condition": condition or {"battery_below": 50},
        "action": action,
        "cooldown_minutes": cooldown_minutes,
    }


def test_rule_state_path_environment_overrides_both_config_forms():
    config = _config(
        rule_state={"path": "/configured/nested.json"},
        rule_state_path="/configured/legacy.json",
    )

    with patch.dict(os.environ, {"PECRON_RULE_STATE_PATH": "/environment/rules.json"}, clear=True):
        monitor = _make_monitor(config=config)
        resolved = monitor._rule_state_path()

    assert resolved == Path("/environment/rules.json")


def test_rule_state_path_nested_config_overrides_legacy_config():
    config = _config(
        rule_state={"path": "/configured/nested.json"},
        rule_state_path="/configured/legacy.json",
    )

    with patch.dict(os.environ, {}, clear=True):
        monitor = _make_monitor(config=config)
        resolved = monitor._rule_state_path()

    assert resolved == Path("/configured/nested.json")


def test_rule_state_path_uses_legacy_top_level_config_when_nested_path_is_missing():
    config = _config(
        rule_state={"initial_state": "normal"},
        rule_state_path="/configured/legacy.json",
    )

    with patch.dict(os.environ, {}, clear=True):
        monitor = _make_monitor(config=config)
        resolved = monitor._rule_state_path()

    assert resolved == Path("/configured/legacy.json")


def test_rule_state_path_defaults_below_home_directory():
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(Path, "home", return_value=Path("/mock-home")),
    ):
        monitor = _make_monitor(config=_config())
        resolved = monitor._rule_state_path()

    assert resolved == Path("/mock-home/.pecron-monitor-rules.json")


def _monitor_with_persisted_content(content):
    config = _config(
        rule_state={
            "path": "/virtual/rules-state.json",
            "initial_state": {"mode": "safe", "charge": "idle"},
        }
    )
    file_handle = mock_open(read_data=content)
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "open", file_handle),
    ):
        monitor = PecronMonitor(config)
    return monitor, file_handle


def test_corrupt_persisted_rule_state_falls_back_to_configured_initial_state(caplog):
    with caplog.at_level(logging.WARNING, logger="pecron"):
        monitor, file_handle = _monitor_with_persisted_content("{not-json")

    assert monitor.rule_states == {"mode": "safe", "charge": "idle"}
    file_handle.assert_called_once_with("r")
    assert "Could not load rule state" in caplog.text


def test_non_dict_persisted_rule_state_falls_back_to_configured_initial_state():
    monitor, _ = _monitor_with_persisted_content('["unsafe", "shape"]')

    assert monitor.rule_states == {"mode": "safe", "charge": "idle"}


def test_empty_persisted_rule_state_falls_back_to_configured_initial_state():
    monitor, _ = _monitor_with_persisted_content("{}")

    assert monitor.rule_states == {"mode": "safe", "charge": "idle"}


def test_rule_cooldown_allows_action_at_exact_equality_and_preserves_shared_entries():
    monitor = _make_monitor([_rule("equality", {"set_ac": False}, cooldown_minutes=5)])
    monitor.last_alert = {"rule_equality": 700.0, "DK": 999.0}

    with patch("monitor_rules.time.time", return_value=1000.0):
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    monitor.set_ac.assert_called_once_with("DK", False)
    assert monitor.last_alert == {"rule_equality": 1000.0, "DK": 999.0}


def test_shared_last_alert_cooldown_is_isolated_per_rule():
    monitor = _make_monitor(
        [
            _rule("cooled", {"set_ac": False}, cooldown_minutes=5),
            _rule("fresh", {"set_dc": True}, cooldown_minutes=5),
        ]
    )
    monitor.last_alert = {"rule_cooled": 950.0, "DK": 975.0}

    with patch("monitor_rules.time.time", return_value=1000.0):
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    monitor.set_ac.assert_not_called()
    monitor.set_dc.assert_called_once_with("DK", True)
    assert monitor.last_alert == {
        "rule_cooled": 950.0,
        "rule_fresh": 1000.0,
        "DK": 975.0,
    }


def test_missing_target_device_gates_all_actions_after_recording_cooldown(caplog):
    action = {
        "device_key": "MISSING",
        "set_ac": False,
        "set_state": "armed",
        "run_command": ["command"],
    }
    monitor = _make_monitor([_rule("missing target", action)])
    monitor._set_rule_states = MagicMock()
    monitor._run_rule_command = MagicMock()

    with (
        patch("monitor_rules.time.time", return_value=1000.0),
        caplog.at_level(logging.WARNING, logger="pecron"),
    ):
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    monitor.set_ac.assert_not_called()
    monitor._set_rule_states.assert_not_called()
    monitor._run_rule_command.assert_not_called()
    assert monitor.last_alert["rule_missing target"] == 1000.0
    assert "target device MISSING not found, skipping" in caplog.text


def test_missing_control_skips_only_that_control_and_continues_other_actions(caplog):
    action = {
        "set_ac": False,
        "set_dc": True,
        "set_state": "armed",
        "run_command": ["command"],
    }
    monitor = _make_monitor([_rule("partial controls", action)], controls={"dc_switch_hm": {}})
    monitor._run_rule_command = MagicMock()

    with (
        patch("monitor_rules.time.time", return_value=1000.0),
        caplog.at_level(logging.WARNING, logger="pecron"),
    ):
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    monitor.set_ac.assert_not_called()
    monitor.set_dc.assert_called_once_with("DK", True)
    assert monitor.rule_states == {"default": "armed"}
    monitor._save_rule_states.assert_called_once_with()
    monitor._run_rule_command.assert_called_once()
    assert "does not have AC control, skipping action" in caplog.text


def test_multiple_actions_execute_in_fixed_control_state_command_order():
    action = {
        "run_command": ["last"],
        "set_state": {"mode": "armed"},
        "set_ups": True,
        "set_dc": False,
        "set_ac": True,
    }
    monitor = _make_monitor([_rule("ordered", action)])
    events = []
    monitor.set_ac.side_effect = lambda device_key, value: events.append(("ac", device_key, value))
    monitor.set_dc.side_effect = lambda device_key, value: events.append(("dc", device_key, value))
    monitor.set_ups.side_effect = lambda device_key, value: events.append(
        ("ups", device_key, value)
    )
    monitor._set_rule_states = MagicMock(side_effect=lambda value: events.append(("state", value)))
    monitor._run_rule_command = MagicMock(
        side_effect=lambda command, **kwargs: events.append(("command", command))
    )

    with patch("monitor_rules.time.time", return_value=1000.0):
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    assert events == [
        ("ac", "DK", True),
        ("dc", "DK", False),
        ("ups", "DK", True),
        ("state", {"mode": "armed"}),
        ("command", ["last"]),
    ]


def test_action_exception_aborts_remaining_actions_but_not_later_rules(caplog):
    rules = [
        _rule("broken", {"set_ac": False, "set_dc": False}),
        _rule("independent", {"set_ups": True}),
    ]
    monitor = _make_monitor(rules)
    monitor.set_ac.side_effect = RuntimeError("control failed")

    with (
        patch("monitor_rules.time.time", return_value=1000.0),
        caplog.at_level(logging.ERROR, logger="pecron"),
    ):
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    monitor.set_ac.assert_called_once_with("DK", False)
    monitor.set_dc.assert_not_called()
    monitor.set_ups.assert_called_once_with("DK", True)
    assert monitor.last_alert == {"rule_broken": 1000.0, "rule_independent": 1000.0}
    assert "Rule evaluation error: control failed" in caplog.text


def test_schedule_requires_exact_current_hour_and_minute_string():
    rules = [
        _rule("exact", {"set_ac": True}, condition={"schedule": "07:05"}),
        _rule("adjacent", {"set_dc": True}, condition={"schedule": "07:04"}),
        _rule("seconds", {"set_ups": True}, condition={"schedule": "07:05:00"}),
    ]
    monitor = _make_monitor(rules)

    with (
        patch("monitor_rules.datetime") as clock,
        patch("monitor_rules.time.time", return_value=1000.0),
    ):
        clock.now.return_value.strftime.return_value = "07:05"
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 40)

    monitor.set_ac.assert_called_once_with("DK", True)
    monitor.set_dc.assert_not_called()
    monitor.set_ups.assert_not_called()
    assert clock.now.return_value.strftime.call_args_list == [
        (("%H:%M",),),
        (("%H:%M",),),
        (("%H:%M",),),
    ]


def _invoke_rule_command(monitor, action):
    monitor._run_rule_command(
        action["run_command"],
        rule={"name": "external"},
        action=action,
        device_key="SOURCE",
        target_device_key="TARGET",
        kv={"voltage": 51.2},
        battery_pct=40,
    )


def test_run_command_nonzero_logs_output_then_raises_runtime_error(caplog):
    monitor = _make_monitor()
    completed = subprocess.CompletedProcess(
        ["runner", "--flag"], 7, stdout="partial output\n", stderr="failure detail\n"
    )

    with (
        patch("monitor_rules.subprocess.run", return_value=completed) as run,
        caplog.at_level(logging.INFO, logger="pecron"),
        pytest.raises(RuntimeError, match="command exited with status 7: runner"),
    ):
        _invoke_rule_command(
            monitor,
            {"run_command": ["runner", "--flag"], "timeout_seconds": 4},
        )

    run.assert_called_once()
    args, kwargs = run.call_args
    assert args == (["runner", "--flag"],)
    assert kwargs["timeout"] == 4.0
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    payload = json.loads(kwargs["input"])
    assert payload["rule"] == "external"
    assert payload["device_key"] == "SOURCE"
    assert payload["target_device_key"] == "TARGET"
    assert "command stdout: partial output" in caplog.text
    assert "command stderr: failure detail" in caplog.text


def test_run_command_timeout_propagates_without_retry_or_control_side_effects():
    monitor = _make_monitor()
    timeout = subprocess.TimeoutExpired(cmd=["slow-command"], timeout=2.5)

    with (
        patch("monitor_rules.subprocess.run", side_effect=timeout) as run,
        pytest.raises(subprocess.TimeoutExpired) as raised,
    ):
        _invoke_rule_command(
            monitor,
            {"run_command": ["slow-command"], "timeout_seconds": 2.5},
        )

    assert raised.value is timeout
    run.assert_called_once()
    assert run.call_args.kwargs["timeout"] == 2.5
    monitor.set_ac.assert_not_called()
    monitor.set_dc.assert_not_called()
    monitor.set_ups.assert_not_called()
