"""Behavior boundaries for output restoration owned by ``PecronMonitor``."""

import json
from contextlib import ExitStack
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import monitor_restore as monitor
from monitor import PecronMonitor


DEVICE_KEY = "DK-RESTORE"


def _make_monitor(restore_cfg):
    instance = PecronMonitor.__new__(PecronMonitor)
    instance.config = {"restore_outputs_after_shutdown": restore_cfg}
    instance.latest_data = {}
    instance._last_offline_at = {}
    instance._last_online_at = {}
    instance._restore_threads = {}
    instance._running = True
    instance.set_ac = MagicMock(return_value=True)
    instance.set_dc = MagicMock(return_value=True)
    return instance


def _snapshot(ac_on=True, dc_on=False, soc=2, age=10):
    snapshot = SimpleNamespace(ac_on=ac_on, dc_on=dc_on, soc_at_offline=soc)
    snapshot.age_seconds = MagicMock(return_value=age)
    return snapshot


def _mqtt_message(value):
    payload = {"deviceKey": DEVICE_KEY, "data": {"value": value}}
    return SimpleNamespace(
        topic="q/2/d/channel/onl_",
        payload=json.dumps(payload).encode("utf-8"),
    )


def test_persisted_snapshot_restores_without_in_memory_offline_timestamp():
    instance = _make_monitor(
        {
            "enabled": True,
            "minimum_offline_seconds": 120,
            "snapshot_max_age_seconds": 3600,
        }
    )
    snapshot = _snapshot(ac_on=True, dc_on=False)
    worker = MagicMock()

    with ExitStack() as stack:
        state = stack.enter_context(patch.object(monitor, "output_state"))
        thread = stack.enter_context(patch.object(monitor.threading, "Thread", return_value=worker))
        stack.enter_context(patch.object(monitor.time, "time", return_value=500.0))
        state.get.return_value = snapshot

        instance._on_device_online(DEVICE_KEY)

    assert DEVICE_KEY not in instance._last_offline_at
    assert instance._last_online_at[DEVICE_KEY] == 500.0
    state.get.assert_called_once_with(DEVICE_KEY)
    thread.assert_called_once_with(
        target=instance._restore_outputs_worker,
        args=(DEVICE_KEY, True, False),
        daemon=True,
        name="restore-DK-RES",
    )
    assert instance._restore_threads[DEVICE_KEY] is worker
    worker.start.assert_called_once_with()


def test_disabled_restore_still_records_online_timestamp_without_reading_snapshot():
    instance = _make_monitor({"enabled": False})

    with ExitStack() as stack:
        state = stack.enter_context(patch.object(monitor, "output_state"))
        thread = stack.enter_context(patch.object(monitor.threading, "Thread"))
        stack.enter_context(patch.object(monitor.time, "time", return_value=725.5))
        instance._on_device_online(DEVICE_KEY)

    assert instance._last_online_at == {DEVICE_KEY: 725.5}
    state.get.assert_not_called()
    thread.assert_not_called()
    assert instance._restore_threads == {}


def test_set_ac_exception_is_isolated_and_dc_restore_continues(caplog):
    instance = _make_monitor({"retry_interval_seconds": 1, "retry_timeout_seconds": 1})
    instance.latest_data[DEVICE_KEY] = {"ac_switch_hm": False, "dc_switch_hm": False}
    instance.set_ac.side_effect = RuntimeError("synthetic AC failure")
    clock = [100.0]

    def advance_clock(_seconds):
        clock[0] += 2.0

    caplog.set_level(logging.WARNING, logger="pecron")
    with ExitStack() as stack:
        state = stack.enter_context(patch.object(monitor, "output_state"))
        stack.enter_context(patch.object(monitor.time, "time", side_effect=lambda: clock[0]))
        sleep = stack.enter_context(patch.object(monitor.time, "sleep", side_effect=advance_clock))
        instance._restore_outputs_worker(DEVICE_KEY, target_ac=True, target_dc=True)

    instance.set_ac.assert_called_once_with(DEVICE_KEY, True)
    instance.set_dc.assert_called_once_with(DEVICE_KEY, True)
    sleep.assert_called_once_with(1)
    state.clear.assert_called_once_with(DEVICE_KEY)
    assert any(
        "set_ac failed" in record.message and "synthetic AC failure" in record.message
        for record in caplog.records
    )


def test_completed_worker_retains_registry_entry_while_clearing_snapshot():
    """Current workers do not remove their completed registry entry themselves."""
    instance = _make_monitor({"retry_timeout_seconds": 60})
    instance.latest_data[DEVICE_KEY] = {"ac_switch_hm": True, "dc_switch_hm": False}
    registered_worker = MagicMock()
    instance._restore_threads[DEVICE_KEY] = registered_worker

    with ExitStack() as stack:
        state = stack.enter_context(patch.object(monitor, "output_state"))
        stack.enter_context(patch.object(monitor.time, "time", return_value=100.0))
        sleep = stack.enter_context(patch.object(monitor.time, "sleep"))
        instance._restore_outputs_worker(DEVICE_KEY, target_ac=True, target_dc=False)

    state.clear.assert_called_once_with(DEVICE_KEY)
    sleep.assert_not_called()
    assert instance._restore_threads == {DEVICE_KEY: registered_worker}


def test_snapshot_save_oserror_is_logged_and_does_not_escape(caplog):
    instance = _make_monitor({"enabled": True, "shutdown_threshold_pct": 10})
    instance.latest_data[DEVICE_KEY] = {
        "battery_percentage": 3,
        "voltage": 47.8,
        "ac_switch_hm": True,
        "dc_switch_hm": False,
    }
    snapshot = object()

    caplog.set_level(logging.WARNING, logger="pecron")
    with ExitStack() as stack:
        state = stack.enter_context(patch.object(monitor, "output_state"))
        stack.enter_context(patch.object(monitor.time, "time", return_value=900.0))
        state.OutputSnapshot.now.return_value = snapshot
        state.save.side_effect = OSError("read-only state directory")

        instance._on_device_offline(DEVICE_KEY)

    assert instance._last_offline_at == {DEVICE_KEY: 900.0}
    state.OutputSnapshot.now.assert_called_once_with(
        ac_on=True,
        dc_on=False,
        soc_at_offline=3,
        voltage_at_offline=47.8,
    )
    state.save.assert_called_once_with(DEVICE_KEY, snapshot)
    instance.set_ac.assert_not_called()
    instance.set_dc.assert_not_called()
    assert any(
        "Could not persist output snapshot" in record.message
        and "read-only state directory" in record.message
        for record in caplog.records
    )


def test_mqtt_online_and_offline_messages_route_to_restore_callbacks():
    instance = _make_monitor({"enabled": True})
    instance._on_device_online = MagicMock()
    instance._on_device_offline = MagicMock()

    instance._on_message(MagicMock(), None, _mqtt_message(1))
    instance._on_message(MagicMock(), None, _mqtt_message(0))

    instance._on_device_online.assert_called_once_with(DEVICE_KEY)
    instance._on_device_offline.assert_called_once_with(DEVICE_KEY)
