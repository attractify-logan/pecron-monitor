"""Tests for issue #59: restore AC/DC switch state after low-battery shutdown.

Covers:
- output_state.py persistence (snapshot save/load/clear, atomic write, age math).
- _on_device_offline: snapshot creation gated on SoC <= shutdown_threshold_pct.
- _on_device_online: minimum_offline_seconds gate, snapshot age guard, worker spawn dedupe.
- _restore_outputs_worker: success path, retry-until-state-matches, timeout.

Hardware testing of the actual low-battery shutdown is constrained: the E1500LFP
on Kim Pine is the live UPS for the workstation (see
feedback_e1500_is_kim_pine_ups.md), so all power-cut behavior is exercised
via these synthetic tests rather than real device drains.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import output_state
from monitor import PecronMonitor


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    p = tmp_path / "pecron-state.json"
    monkeypatch.setenv("PECRON_STATE_PATH", str(p))
    return p


def _make_monitor(restore_cfg=None):
    m = PecronMonitor.__new__(PecronMonitor)
    m.config = {"alerts": {}, "rules": []}
    if restore_cfg is not None:
        m.config["restore_outputs_after_shutdown"] = restore_cfg
    m.devices = [{"device_key": "DK", "device_name": "TestDevice"}]
    m.latest_data = {}
    m.data_sources = {}
    m._last_logged_values = {}
    m.last_alert = {}
    m.last_rule_action = {}
    m.rules = []
    m._running = True
    m._last_offline_at = {}
    m._last_online_at = {}
    m._restore_threads = {}
    m.ha_bridge = MagicMock()
    m.set_ac = MagicMock(return_value=True)
    m.set_dc = MagicMock(return_value=True)
    return m


# -------------------- output_state persistence ----------------------


class TestOutputStatePersistence:
    def test_save_and_load_round_trip(self, tmp_state_path):
        snap = output_state.OutputSnapshot.now(ac_on=True, dc_on=False, soc_at_offline=3)
        output_state.save("DK1", snap)
        loaded = output_state.get("DK1")
        assert loaded is not None
        assert loaded.ac_on is True
        assert loaded.dc_on is False
        assert loaded.soc_at_offline == 3

    def test_save_overwrites_same_device(self, tmp_state_path):
        output_state.save("DK", output_state.OutputSnapshot.now(True, True, 5))
        output_state.save("DK", output_state.OutputSnapshot.now(False, False, 1))
        assert output_state.get("DK").ac_on is False
        assert output_state.get("DK").dc_on is False

    def test_clear_removes_entry(self, tmp_state_path):
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 5))
        assert output_state.clear("DK") is True
        assert output_state.get("DK") is None

    def test_clear_nonexistent_returns_false(self, tmp_state_path):
        assert output_state.clear("NOPE") is False

    def test_load_missing_file_returns_empty(self, tmp_state_path):
        assert output_state.load_all() == {}

    def test_load_corrupt_file_returns_empty(self, tmp_state_path, caplog):
        tmp_state_path.write_text("not valid json")
        caplog.set_level(logging.WARNING, logger="pecron")
        assert output_state.load_all() == {}

    def test_load_skips_malformed_entry(self, tmp_state_path):
        tmp_state_path.write_text(json.dumps({
            "snapshots": {
                "GOOD": {"ac_on": True, "dc_on": False,
                         "soc_at_offline": 5, "snapshotted_at": "2026-05-03T00:00:00+00:00"},
                "BAD": {"ac_on": True},  # missing fields
            }
        }))
        snaps = output_state.load_all()
        assert "GOOD" in snaps
        assert "BAD" not in snaps

    def test_atomic_write_does_not_leave_temp_files(self, tmp_state_path, tmp_path):
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 5))
        assert not list(tmp_path.glob(".pecron-state-*"))

    def test_age_seconds_for_recent_snapshot(self, tmp_state_path):
        snap = output_state.OutputSnapshot.now(True, False, 5)
        assert snap.age_seconds() < 5

    def test_age_seconds_for_malformed_timestamp(self, tmp_state_path):
        snap = output_state.OutputSnapshot(
            ac_on=True, dc_on=False, soc_at_offline=5,
            snapshotted_at="not-a-date",
        )
        assert snap.age_seconds() == float("inf")


# -------------------- offline event -> snapshot -----------------------


class TestOnDeviceOffline:
    def test_disabled_does_nothing(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": False})
        m.latest_data["DK"] = {
            "battery_percentage": 2,
            "ac_switch_hm": True, "dc_switch_hm": False,
        }
        m._on_device_offline("DK")
        assert output_state.get("DK") is None

    def test_below_threshold_snapshots(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": True, "shutdown_threshold_pct": 10})
        m.latest_data["DK"] = {
            "battery_percentage": 2,
            "ac_switch_hm": True, "dc_switch_hm": False,
        }
        m._on_device_offline("DK")
        snap = output_state.get("DK")
        assert snap is not None
        assert snap.ac_on is True
        assert snap.dc_on is False
        assert snap.soc_at_offline == 2

    def test_above_threshold_no_snapshot(self, tmp_state_path):
        # Manual unplug at 50% — not a low-battery shutdown.
        m = _make_monitor(restore_cfg={"enabled": True, "shutdown_threshold_pct": 10})
        m.latest_data["DK"] = {"battery_percentage": 50, "ac_switch_hm": True}
        m._on_device_offline("DK")
        assert output_state.get("DK") is None

    def test_no_soc_no_snapshot(self, tmp_state_path):
        # Cold boot offline before any telemetry — can't tell if low-battery.
        m = _make_monitor(restore_cfg={"enabled": True})
        m._on_device_offline("DK")
        assert output_state.get("DK") is None

    def test_uses_host_packet_soc_when_top_level_absent(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": True, "shutdown_threshold_pct": 10})
        m.latest_data["DK"] = {
            "host_packet_data_jdb": {"host_packet_electric_percentage": 3},
            "ac_switch_hm": True, "dc_switch_hm": True,
        }
        m._on_device_offline("DK")
        snap = output_state.get("DK")
        assert snap is not None
        assert snap.soc_at_offline == 3
        assert snap.dc_on is True

    def test_string_switch_state_coerces_to_bool(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": True, "shutdown_threshold_pct": 10})
        m.latest_data["DK"] = {
            "battery_percentage": 2,
            "ac_switch_hm": "ON", "dc_switch_hm": "OFF",
        }
        m._on_device_offline("DK")
        snap = output_state.get("DK")
        assert snap.ac_on is True
        assert snap.dc_on is False

    def test_records_offline_timestamp(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": False})  # disabled is fine — we test the timestamp
        before = time.time()
        m._on_device_offline("DK")
        assert before <= m._last_offline_at["DK"] <= time.time()


# -------------------- online event -> restore worker ------------------


class TestOnDeviceOnline:
    def test_disabled_does_nothing(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": False})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m._on_device_online("DK")
        assert "DK" not in m._restore_threads

    def test_no_snapshot_no_worker(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": True})
        m._on_device_online("DK")
        assert "DK" not in m._restore_threads

    def test_snapshot_too_brief_offline_skips(self, tmp_state_path):
        # Online transition only 5s after offline transition: network blip,
        # not a real shutdown. Don't fire restore.
        m = _make_monitor(restore_cfg={"enabled": True, "minimum_offline_seconds": 120})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m._last_offline_at["DK"] = time.time() - 5
        m._on_device_online("DK")
        assert "DK" not in m._restore_threads
        # Snapshot should NOT be cleared in this path — a future, longer offline
        # should still get a chance to fire restore.
        assert output_state.get("DK") is not None

    def test_stale_snapshot_discarded(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": True, "snapshot_max_age_seconds": 60})
        snap = output_state.OutputSnapshot(
            ac_on=True, dc_on=False, soc_at_offline=2,
            snapshotted_at="2020-01-01T00:00:00+00:00",  # ancient
        )
        output_state.save("DK", snap)
        m._on_device_online("DK")
        assert "DK" not in m._restore_threads
        assert output_state.get("DK") is None  # cleared

    def test_long_offline_with_snapshot_spawns_worker(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"enabled": True, "minimum_offline_seconds": 120,
                                       "retry_interval_seconds": 1,
                                       "retry_timeout_seconds": 5})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m._last_offline_at["DK"] = time.time() - 600  # 10 min ago

        # Pre-seed latest_data so the worker's first iteration sees the target
        # state already matched and exits quickly.
        m.latest_data["DK"] = {"ac_switch_hm": True, "dc_switch_hm": False}

        m._on_device_online("DK")
        t = m._restore_threads.get("DK")
        assert t is not None
        t.join(timeout=3)
        assert not t.is_alive()
        # Snapshot should be cleared once worker confirms target state.
        assert output_state.get("DK") is None

    def test_worker_dedup_on_duplicate_online(self, tmp_state_path):
        # If two online events arrive in rapid succession (cloud flap), only
        # one worker should be running for the device.
        m = _make_monitor(restore_cfg={"enabled": True, "minimum_offline_seconds": 1,
                                       "retry_interval_seconds": 10,
                                       "retry_timeout_seconds": 30})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m._last_offline_at["DK"] = time.time() - 10
        m.latest_data["DK"] = {"ac_switch_hm": False, "dc_switch_hm": False}

        m._on_device_online("DK")
        first = m._restore_threads["DK"]
        m._on_device_online("DK")  # second event
        second = m._restore_threads["DK"]
        assert first is second  # same thread, no respawn
        # Stop the worker to let the test exit.
        m._running = False
        first.join(timeout=15)


# -------------------- restore worker: retry semantics -----------------


class TestRestoreWorker:
    def test_already_in_target_state_clears_snapshot(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"retry_interval_seconds": 1,
                                       "retry_timeout_seconds": 5})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m.latest_data["DK"] = {"ac_switch_hm": True, "dc_switch_hm": False}
        m._restore_outputs_worker("DK", target_ac=True, target_dc=False)
        assert output_state.get("DK") is None
        m.set_ac.assert_not_called()
        m.set_dc.assert_not_called()

    def test_issues_set_ac_when_state_differs(self, tmp_state_path):
        # Latest data shows AC=False; target is True. After the first iteration,
        # we update latest_data to True so the second iteration exits.
        m = _make_monitor(restore_cfg={"retry_interval_seconds": 0.05,
                                       "retry_timeout_seconds": 2})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m.latest_data["DK"] = {"ac_switch_hm": False, "dc_switch_hm": False}

        # set_ac side-effect: flip the cached state so the next iteration
        # observes the new value, mimicking telemetry catching up.
        def _flip_ac(dk, on):
            m.latest_data[dk]["ac_switch_hm"] = on
            return True
        m.set_ac = MagicMock(side_effect=_flip_ac)

        m._restore_outputs_worker("DK", target_ac=True, target_dc=False)
        assert m.set_ac.call_count >= 1
        m.set_ac.assert_any_call("DK", True)
        assert output_state.get("DK") is None

    def test_issues_set_dc_when_state_differs(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"retry_interval_seconds": 0.05,
                                       "retry_timeout_seconds": 2})
        m.latest_data["DK"] = {"ac_switch_hm": False, "dc_switch_hm": False}

        def _flip_dc(dk, on):
            m.latest_data[dk]["dc_switch_hm"] = on
            return True
        m.set_dc = MagicMock(side_effect=_flip_dc)

        m._restore_outputs_worker("DK", target_ac=False, target_dc=True)
        m.set_dc.assert_any_call("DK", True)

    def test_timeout_clears_snapshot_and_logs_error(self, tmp_state_path, caplog):
        # set_ac always succeeds at the API layer but latest_data never
        # reflects the change (mimics the LCD-at-0% silent rejection). After
        # timeout, snapshot should be cleared and an ERROR logged.
        m = _make_monitor(restore_cfg={"retry_interval_seconds": 0.05,
                                       "retry_timeout_seconds": 0.2})
        output_state.save("DK", output_state.OutputSnapshot.now(True, False, 2))
        m.latest_data["DK"] = {"ac_switch_hm": False, "dc_switch_hm": False}
        # set_ac is a no-op (default MagicMock) — latest_data stays False.

        caplog.set_level(logging.ERROR, logger="pecron")
        m._restore_outputs_worker("DK", target_ac=True, target_dc=False)
        assert output_state.get("DK") is None
        assert any("timed out" in r.message for r in caplog.records)

    def test_monitor_shutdown_aborts_worker(self, tmp_state_path):
        m = _make_monitor(restore_cfg={"retry_interval_seconds": 10,
                                       "retry_timeout_seconds": 60})
        m.latest_data["DK"] = {"ac_switch_hm": False, "dc_switch_hm": False}
        m._running = False  # simulate shutdown before worker starts
        m._restore_outputs_worker("DK", target_ac=True, target_dc=False)
        m.set_ac.assert_not_called()
