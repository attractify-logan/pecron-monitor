"""Tests for compound (ANDed) rule conditions, output-power conditions, and
time-window scheduling (#56)."""

from unittest.mock import MagicMock, patch

from monitor import PecronMonitor


def _monitor_with_rule(condition, action=None):
    config = {
        "email": "test@example.com",
        "password": "test",
        "region": "na",
        "devices": [],
        "rules": [
            {
                "name": "compound rule",
                "condition": condition,
                "action": action or {"set_ac": False},
                "cooldown_minutes": 0,
            }
        ],
    }
    monitor = PecronMonitor(config)
    monitor.devices = [
        {"device_key": "DK", "device_name": "TestDevice", "controls": {"ac_switch_hm": {}}}
    ]
    monitor.set_ac = MagicMock(return_value=True)
    return monitor


# --- compound AND semantics -------------------------------------------------


def test_compound_all_conditions_met_fires():
    # Bruce's scenario: charge only when battery is low AND load is not high.
    monitor = _monitor_with_rule({"voltage_below": 50.5, "output_power_below": 2000})
    monitor._evaluate_rules("DK", {"voltage": 50.0, "total_output_power": 800}, 30)
    monitor.set_ac.assert_called_once_with("DK", False)


def test_compound_one_condition_unmet_does_not_fire():
    # Voltage low but load too high -> must NOT fire (would overload the inverter).
    monitor = _monitor_with_rule({"voltage_below": 50.5, "output_power_below": 2000})
    monitor._evaluate_rules("DK", {"voltage": 50.0, "total_output_power": 3000}, 30)
    monitor.set_ac.assert_not_called()


def test_compound_other_condition_unmet_does_not_fire():
    monitor = _monitor_with_rule({"voltage_below": 50.5, "output_power_below": 2000})
    monitor._evaluate_rules("DK", {"voltage": 52.0, "total_output_power": 800}, 30)
    monitor.set_ac.assert_not_called()


def test_compound_three_conditions():
    monitor = _monitor_with_rule(
        {"battery_below": 40, "voltage_below": 50.5, "output_power_below": 2000}
    )
    monitor._evaluate_rules("DK", {"voltage": 50.0, "total_output_power": 800}, 35)
    monitor.set_ac.assert_called_once_with("DK", False)


def test_compound_missing_telemetry_skips_rule():
    # voltage clause unevaluable (no voltage) -> whole rule must not fire.
    monitor = _monitor_with_rule({"voltage_below": 50.5, "output_power_below": 2000})
    monitor._evaluate_rules("DK", {"total_output_power": 800}, 30)
    monitor.set_ac.assert_not_called()


# --- output-power conditions ------------------------------------------------


def test_output_power_above_triggers():
    monitor = _monitor_with_rule({"output_power_above": 2500})
    monitor._evaluate_rules("DK", {"voltage": 52.0, "total_output_power": 3000}, 50)
    monitor.set_ac.assert_called_once_with("DK", False)


def test_output_power_below_triggers():
    monitor = _monitor_with_rule({"output_power_below": 100})
    monitor._evaluate_rules("DK", {"voltage": 52.0, "total_output_power": 50}, 50)
    monitor.set_ac.assert_called_once_with("DK", False)


# --- state-gate-only must never fire (regression for vacuous-AND bug) --------


def test_state_gate_without_trigger_never_fires():
    monitor = _monitor_with_rule({"state": "peak"})
    monitor.rule_state = "peak"
    monitor._evaluate_rules("DK", {"voltage": 50.0, "total_output_power": 800}, 30)
    monitor.set_ac.assert_not_called()


# --- time-window logic (pure) -----------------------------------------------


def test_in_time_window_normal():
    assert PecronMonitor._in_time_window("18:30", "17:00", "21:00") is True
    assert PecronMonitor._in_time_window("16:59", "17:00", "21:00") is False
    assert PecronMonitor._in_time_window("17:00", "17:00", "21:00") is True  # inclusive start
    assert PecronMonitor._in_time_window("21:00", "17:00", "21:00") is False  # exclusive end


def test_in_time_window_wraps_midnight():
    assert PecronMonitor._in_time_window("23:30", "22:00", "06:00") is True
    assert PecronMonitor._in_time_window("02:00", "22:00", "06:00") is True
    assert PecronMonitor._in_time_window("12:00", "22:00", "06:00") is False


def test_in_time_window_zero_width_and_invalid():
    assert PecronMonitor._in_time_window("10:00", "10:00", "10:00") is False
    assert PecronMonitor._in_time_window("nope", "17:00", "21:00") is False


# --- schedule_between wiring through evaluate --------------------------------


def test_schedule_between_inside_window_fires():
    monitor = _monitor_with_rule({"schedule_between": ["17:00", "21:00"]})
    with patch("monitor.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "18:00"
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 50)
    monitor.set_ac.assert_called_once_with("DK", False)


def test_schedule_between_outside_window_does_not_fire():
    monitor = _monitor_with_rule({"schedule_between": ["17:00", "21:00"]})
    with patch("monitor.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "09:00"
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 50)
    monitor.set_ac.assert_not_called()


def test_schedule_between_compound_with_voltage():
    # Peak window AND low voltage -> fire; outside window -> don't.
    cond = {"schedule_between": ["17:00", "21:00"], "voltage_below": 50.5}
    monitor = _monitor_with_rule(cond)
    with patch("monitor.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "18:00"
        monitor._evaluate_rules("DK", {"voltage": 50.0}, 30)
    monitor.set_ac.assert_called_once_with("DK", False)


def test_schedule_between_malformed_does_not_fire():
    monitor = _monitor_with_rule({"schedule_between": ["17:00"]})
    with patch("monitor.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "18:00"
        monitor._evaluate_rules("DK", {"voltage": 52.0}, 50)
    monitor.set_ac.assert_not_called()
