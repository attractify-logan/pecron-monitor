"""Output-restore policy for :class:`monitor.PecronMonitor`."""

import logging
import threading
import time

import output_state
from monitor_data import coerce_switch, extract_soc, extract_voltage


log = logging.getLogger("pecron")


class MonitorRestoreMixin:
    def _restore_cfg(self) -> dict:
        return self.config.get("restore_outputs_after_shutdown", {}) or {}

    def _on_device_offline(self, device_key: str):
        """Called when a `is now offline` event arrives.

        Snapshot the user's current AC/DC switch state to disk if telemetry
        indicates a low-battery shutdown. Percentage remains the default
        threshold; voltage can be configured for LFP packs whose SoC estimate
        drifts near empty.
        """
        now = time.time()
        self._last_offline_at[device_key] = now
        log.info("Device %s is now offline", device_key)

        cfg = self._restore_cfg()
        if not cfg.get("enabled", False):
            return

        kv = self.latest_data.get(device_key, {})
        soc = extract_soc(kv)
        voltage = extract_voltage(kv)
        threshold_pct = int(cfg.get("shutdown_threshold_pct", 10))
        threshold_voltage = cfg.get("shutdown_threshold_voltage")
        if threshold_voltage is not None:
            threshold_voltage = float(threshold_voltage)

        pct_crossed = soc is not None and soc <= threshold_pct
        voltage_crossed = (
            threshold_voltage is not None and voltage is not None and voltage <= threshold_voltage
        )
        if not pct_crossed and not voltage_crossed:
            if soc is None and (threshold_voltage is None or voltage is None):
                log.debug(
                    "No usable SoC/voltage observation for %s at offline transition; "
                    "skipping snapshot",
                    device_key,
                )
            else:
                log.debug(
                    "Device %s went offline at SoC=%s%% voltage=%sV; thresholds "
                    "are SoC<=%d%% voltage<=%sV; skipping snapshot.",
                    device_key,
                    soc if soc is not None else "unknown",
                    f"{voltage:.1f}" if voltage is not None else "unknown",
                    threshold_pct,
                    threshold_voltage if threshold_voltage is not None else "disabled",
                )
            return

        ac_on = coerce_switch(kv.get("ac_switch_hm"))
        dc_on = coerce_switch(kv.get("dc_switch_hm"))
        # If either switch state is unobservable, snapshot what we have (default
        # missing to False rather than refuse to snapshot — restore worker will
        # only act on differences from observed live state anyway).
        snap = output_state.OutputSnapshot.now(
            ac_on=bool(ac_on) if ac_on is not None else False,
            dc_on=bool(dc_on) if dc_on is not None else False,
            soc_at_offline=soc,
            voltage_at_offline=voltage,
        )
        try:
            output_state.save(device_key, snap)
        except OSError as e:
            log.warning("Could not persist output snapshot for %s: %s", device_key, e)
            return
        log.info(
            "Snapshotted output state for %s for shutdown-restore "
            "(SoC=%s%%, voltage=%sV, AC=%s, DC=%s)",
            device_key,
            soc if soc is not None else "unknown",
            f"{voltage:.1f}" if voltage is not None else "unknown",
            ac_on,
            dc_on,
        )

    def _on_device_online(self, device_key: str):
        """Called when a `is now online` event arrives. If a snapshot exists
        and the offline gap was long enough to be a real shutdown, spawns a
        background worker to restore the AC/DC state."""
        now = time.time()
        self._last_online_at[device_key] = now
        log.info("Device %s is now online", device_key)

        cfg = self._restore_cfg()
        if not cfg.get("enabled", False):
            return

        snap = output_state.get(device_key)
        if snap is None:
            return  # No snapshot, no restore.

        max_age = int(cfg.get("snapshot_max_age_seconds", 86400))
        age = snap.age_seconds()
        if age > max_age:
            log.warning(
                "Discarding stale output snapshot for %s (age %.0fs > max %ds)",
                device_key,
                age,
                max_age,
            )
            output_state.clear(device_key)
            return

        min_offline = int(cfg.get("minimum_offline_seconds", 120))
        last_offline = self._last_offline_at.get(device_key)
        if last_offline is not None and (now - last_offline) < min_offline:
            log.info(
                "Online transition for %s within %.1fs of last offline (<%d "
                "minimum) — too brief to be a real shutdown, skipping restore.",
                device_key,
                now - last_offline,
                min_offline,
            )
            return

        existing = self._restore_threads.get(device_key)
        if existing is not None and existing.is_alive():
            log.debug("Restore worker already running for %s; skipping duplicate spawn", device_key)
            return

        log.info(
            "Starting restore worker for %s — target AC=%s, DC=%s "
            "(snapshot taken %.0fs ago at SoC=%d%%)",
            device_key,
            snap.ac_on,
            snap.dc_on,
            age,
            snap.soc_at_offline,
        )
        t = threading.Thread(
            target=self._restore_outputs_worker,
            args=(device_key, bool(snap.ac_on), bool(snap.dc_on)),
            daemon=True,
            name=f"restore-{device_key[:6]}",
        )
        self._restore_threads[device_key] = t
        t.start()

    def _restore_outputs_worker(self, device_key: str, target_ac: bool, target_dc: bool):
        """Retry loop: every `retry_interval_seconds` (default 30s), check if
        observed AC/DC state matches the target and re-issue commands if not.
        Bail out when both match (success), when timeout elapses, or when the
        monitor is shutting down. State verification is via observed
        latest_data — robust against the LCD-at-0%-silently-rejects pattern
        Bruce documented in #57."""
        cfg = self._restore_cfg()
        interval = max(1, int(cfg.get("retry_interval_seconds", 30)))
        timeout = int(cfg.get("retry_timeout_seconds", 600))
        started_at = time.time()

        while time.time() - started_at < timeout:
            if not self._running:
                log.info("Monitor shutting down; restore worker for %s exiting", device_key)
                return

            kv = self.latest_data.get(device_key, {}) or {}
            current_ac = coerce_switch(kv.get("ac_switch_hm"))
            current_dc = coerce_switch(kv.get("dc_switch_hm"))

            if current_ac == target_ac and current_dc == target_dc:
                log.info("Restore complete for %s: AC=%s DC=%s", device_key, target_ac, target_dc)
                output_state.clear(device_key)
                return

            if current_ac != target_ac:
                log.info(
                    "Restore: setting AC=%s on %s (currently %s)", target_ac, device_key, current_ac
                )
                try:
                    self.set_ac(device_key, target_ac)
                except Exception as e:
                    log.warning("Restore: set_ac failed for %s: %s", device_key, e)

            if current_dc != target_dc:
                log.info(
                    "Restore: setting DC=%s on %s (currently %s)", target_dc, device_key, current_dc
                )
                try:
                    self.set_dc(device_key, target_dc)
                except Exception as e:
                    log.warning("Restore: set_dc failed for %s: %s", device_key, e)

            time.sleep(interval)

        kv = self.latest_data.get(device_key, {}) or {}
        log.error(
            "Restore for %s timed out after %ds; clearing snapshot. "
            "Target was AC=%s DC=%s; current is AC=%s DC=%s",
            device_key,
            timeout,
            target_ac,
            target_dc,
            coerce_switch(kv.get("ac_switch_hm")),
            coerce_switch(kv.get("dc_switch_hm")),
        )
        output_state.clear(device_key)
