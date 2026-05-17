"""Tests for voltage-based automation rule conditions (#56/#57)."""

from unittest.mock import MagicMock

from monitor import PecronMonitor


def _monitor_with_rule(condition):
    config = {
        "email": "test@example.com",
        "password": "test",
        "region": "na",
        "devices": [],
        "rules": [
            {
                "name": "voltage rule",
                "condition": condition,
                "action": {"set_ac": False},
                "cooldown_minutes": 0,
            }
        ],
    }
    monitor = PecronMonitor(config)
    monitor.devices = [
        {
            "device_key": "DK",
            "device_name": "TestDevice",
            "controls": {"ac_switch_hm": {}},
        }
    ]
    monitor.set_ac = MagicMock(return_value=True)
    return monitor


def test_voltage_below_triggers_action():
    monitor = _monitor_with_rule({"voltage_below": 48.0})

    monitor._evaluate_rules("DK", {"battery_percentage": 50, "voltage": 47.9}, 50)

    monitor.set_ac.assert_called_once_with("DK", False)


def test_voltage_below_does_not_trigger_above_threshold():
    monitor = _monitor_with_rule({"voltage_below": 48.0})

    monitor._evaluate_rules("DK", {"battery_percentage": 50, "voltage": 48.1}, 50)

    monitor.set_ac.assert_not_called()


def test_voltage_above_triggers_action():
    monitor = _monitor_with_rule({"voltage_above": 54.0})

    monitor._evaluate_rules("DK", {"battery_percentage": 90, "voltage": 54.1}, 90)

    monitor.set_ac.assert_called_once_with("DK", False)


def test_missing_voltage_does_not_trigger():
    monitor = _monitor_with_rule({"voltage_below": 48.0})

    monitor._evaluate_rules("DK", {"battery_percentage": 50}, 50)

    monitor.set_ac.assert_not_called()
