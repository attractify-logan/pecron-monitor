"""One-shot command and status presentation for :class:`PecronMonitor`."""

import logging
import time
from typing import Optional

from constants import BATTERY_CAPACITY_WH, DEVICE_STATUS_LABELS, SENSOR_FIELDS
from helpers import _get_kv

log = logging.getLogger("pecron")


class MonitorStatusMixin:
    def one_shot_command(self, ac=None, dc=None, force_offline=False):
        """Connect, send a command, verify, and exit."""
        self.authenticate(force_offline=force_offline)
        if not self.offline_mode:
            self.connect_mqtt()
            time.sleep(3)
        else:
            # In offline mode, explicitly connect any local transports before sending controls
            for device in self.devices:
                dk = device["device_key"]
                self._connect_local(dk)
            time.sleep(1)  # Give local transports time to connect

        for device in self.devices:
            dk = device["device_key"]
            if ac is not None:
                self.set_ac(dk, ac)
            if dc is not None:
                self.set_dc(dk, dc)

        time.sleep(3)
        # Read back state to confirm
        self._request_status()
        time.sleep(5)

        for dk, kv in self.latest_data.items():
            ac_state = "ON" if kv.get("ac_switch_hm") else "OFF"
            dc_state = "ON" if kv.get("dc_switch_hm") else "OFF"
            print(f"Device {dk}: AC={ac_state} DC={dc_state}")

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

    def status_once(self, force_offline: bool = False, skip_local: Optional[bool] = None):
        self.authenticate(force_offline=force_offline, skip_local=skip_local)
        if not self.offline_mode:
            self.connect_mqtt()
            time.sleep(3)
            # Enable high-freq reporting for devices that need it (E3600/E3800)
            # Stagger requests to avoid cloud throttling for multi-device setups.
            # Use the shared helper so the per-model skip (issue #14) applies here too.
            if self.mqtt_client:
                self._enable_high_freq_reporting(stagger=1)
                time.sleep(2)  # Give devices time to switch modes
        self._request_status()

        # E3600/E3800 sends telemetry in alternating MQTT packets at ~10-15s intervals.
        # Use an active polling loop: check every 5s, re-request for incomplete devices,
        # give up after max_wait seconds total.
        max_wait = 45  # Total max wait (was 30s rigid, now smarter)
        check_interval = 5
        elapsed = 0
        all_device_keys = {d["device_key"] for d in self.devices}
        last_request_time = 0

        log.info(
            "Collecting data (up to %ds for multi-packet devices like E3600/E3800)...", max_wait
        )

        while elapsed < max_wait:
            time.sleep(check_interval)
            elapsed += check_interval

            # Check which devices still need telemetry
            incomplete = []
            for dk in all_device_keys:
                kv = self.latest_data.get(dk, {})
                if not self._has_telemetry_fields(kv):
                    incomplete.append(dk)

            if not incomplete:
                log.info("All devices have telemetry data (%ds)", elapsed)
                break

            # Re-request data for incomplete devices every 10s
            if elapsed - last_request_time >= 10:
                log.info(
                    "Still waiting for telemetry from: %s (%ds/%ds)...",
                    ", ".join(incomplete),
                    elapsed,
                    max_wait,
                )
                # Re-attempt via the normal status path (issue #84: this used to
                # only re-publish MQTT read requests, so --local/offline mode --
                # which has no mqtt_client -- never retried local TCP at all and
                # just idled out the full 45s on a single initial attempt).
                # _request_status() covers BLE/local TCP/MQTT-publish/REST for
                # every device and is a no-op for any transport that isn't
                # configured, so this is safe to call unconditionally here.
                self._request_status()
                last_request_time = elapsed

        if elapsed >= max_wait:
            incomplete = [
                dk
                for dk in all_device_keys
                if not self._has_telemetry_fields(self.latest_data.get(dk, {}))
            ]
            if incomplete:
                log.warning("Timed out waiting for telemetry from: %s", ", ".join(incomplete))

        def _norm_model_key(value: str) -> str:
            return "".join(ch for ch in str(value).upper() if ch.isalnum())

        capacity_lookup = {
            _norm_model_key(model): float(capacity)
            for model, capacity in BATTERY_CAPACITY_WH.items()
        }

        def _fmt_hours(hours: float) -> str:
            if hours < 0:
                return "N/A"
            total_min = int(hours * 60)
            days, rem = divmod(total_min, 24 * 60)
            h, m = divmod(rem, 60)
            if days > 0:
                return f"{days}d {h}h {m}m"
            return f"{h}h {m}m"

        for dk, kv in self.latest_data.items():
            source = self.data_sources.get(dk, "UNKNOWN")

            # Compute total power with AC+DC fallback
            total_in = int(_get_kv(kv, SENSOR_FIELDS["total_input_power"], 0))
            total_out = int(_get_kv(kv, SENSOR_FIELDS["total_output_power"], 0))
            if total_in == 0:
                ac_in = int(_get_kv(kv, SENSOR_FIELDS["ac_input_power"], 0))
                dc_in = int(_get_kv(kv, SENSOR_FIELDS["dc_input_power"], 0))
                if ac_in + dc_in > 0:
                    total_in = ac_in + dc_in
            if total_out == 0:
                ac_out = int(_get_kv(kv, SENSOR_FIELDS["ac_output_power"], 0))
                dc_out = int(_get_kv(kv, SENSOR_FIELDS["dc_output_power"], 0))
                if ac_out + dc_out > 0:
                    total_out = ac_out + dc_out

            # Compute net power drain on host battery
            net_drain = float(_get_kv(kv, SENSOR_FIELDS["voltage"], 0)) * float(
                _get_kv(kv, SENSOR_FIELDS["current"], 0)
            )
            net_drain_label = "Net Drain: " if net_drain < 0 else "Net Charge:"

            remain = int(_get_kv(kv, SENSOR_FIELDS["remain_time"], 0))
            # Check if remain_time is unreliable (local transports often return bogus values)
            battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
            if source in ("LOCAL TCP", "BLE") and remain <= 5 and battery_pct > 50:
                remain_str = "N/A (unreliable from local)"
            else:
                remain_str = f"{remain // 60}h {remain % 60}m" if remain > 0 else "N/A"

            charge_remain = int(_get_kv(kv, SENSOR_FIELDS["remain_charging_time"], 0))
            charge_remain_str = (
                f"{charge_remain // 60}h {charge_remain % 60}m" if charge_remain > 0 else "N/A"
            )

            # Compute estimated time-to-empty/full from capacity table + battery % + net battery power.
            model_info = self._find_device(dk)
            model_candidates = [
                model_info.get("product_name", ""),
                model_info.get("device_name", ""),
            ]
            capacity_wh = None
            for candidate in model_candidates:
                key = _norm_model_key(candidate)
                if key in capacity_lookup:
                    capacity_wh = capacity_lookup[key]
                    break

            est_time_str = "N/A"
            if capacity_wh and 0 <= battery_pct <= 100:
                current_charge_wh = capacity_wh * (battery_pct / 100.0)
                net_power_w = net_drain  # <0 discharge, >0 charge
                if net_power_w != 0:
                    if net_power_w < 0:
                        est_time_str = _fmt_hours(current_charge_wh / abs(net_power_w))
                    else:
                        est_time_str = _fmt_hours((capacity_wh - current_charge_wh) / net_power_w)

            status_value = int(_get_kv(kv, SENSOR_FIELDS["device_status_hm"], -1))
            status_str = (
                DEVICE_STATUS_LABELS.get(int(status_value), str(status_value))
                if status_value >= 0
                else "Unknown"
            )

            print(f"\n{'=' * 50}")
            print(f"Device: {dk}")
            print(f"Connection: {source}")
            print(f"{'=' * 50}")
            print(f"Status:        {status_str} ({status_value})")
            print(f"Battery:       {_get_kv(kv, SENSOR_FIELDS['battery_percent'], '?')}%")
            print(f"Voltage:       {float(_get_kv(kv, SENSOR_FIELDS['voltage'], 0)):.1f}V")
            print(f"Temperature:   {_get_kv(kv, SENSOR_FIELDS['temperature'], '?')}°C")
            print(f"Discharge time:{remain_str}")
            print(f"Charge time:   {charge_remain_str}")
            print(f"{net_drain_label}    {abs(net_drain):.1f}W")
            if capacity_wh:
                print(f"Capacity:      {capacity_wh:.0f}Wh")
                if net_drain < 0:
                    print(f"Est. Empty:    {est_time_str}")
                elif net_drain > 0:
                    print(f"Est. Full:     {est_time_str}")
            else:
                print("Capacity:      Unknown (not in BATTERY_CAPACITY_WH)")
            print(f"Total Input:   {total_in}W")
            print(f"Total Output:  {total_out}W")
            print(
                f"AC Output:     {_get_kv(kv, SENSOR_FIELDS['ac_output_power'], 0)}W @ {_get_kv(kv, SENSOR_FIELDS['ac_output_voltage'], '?')}V"
            )
            print(f"DC Output:     {_get_kv(kv, SENSOR_FIELDS['dc_output_power'], 0)}W")
            print(f"AC Input:      {_get_kv(kv, SENSOR_FIELDS['ac_input_power'], 0)}W")
            print(f"DC Input:      {_get_kv(kv, SENSOR_FIELDS['dc_input_power'], 0)}W")
            print(f"AC Switch:     {'ON' if _get_kv(kv, SENSOR_FIELDS['ac_switch']) else 'OFF'}")
            print(f"DC Switch:     {'ON' if _get_kv(kv, SENSOR_FIELDS['dc_switch']) else 'OFF'}")
            print(f"UPS Mode:      {'ON' if _get_kv(kv, SENSOR_FIELDS['ups_mode']) else 'OFF'}")

            packs = kv.get("charging_pack_data_jdb", [])
            for i, pack in enumerate(packs):
                try:
                    pack_status_val = int(float(pack.get("charging_pack_status", 4)))
                except (ValueError, TypeError):
                    pack_status_val = 4
                if pack_status_val != 4:
                    # Fallback: some devices report battery % in charging_pack_status instead
                    try:
                        pack_battery = int(float(pack.get("charging_pack_battery", 0)))
                    except (ValueError, TypeError):
                        pack_battery = 0
                    if pack_battery == 0 and 5 <= pack_status_val <= 100:
                        pack_battery = pack_status_val
                        log.debug(
                            "Using charging_pack_status (%d%%) as battery for pack %d",
                            pack_status_val,
                            i,
                        )
                    print(
                        f"Pack {i}:        {pack_battery if pack_battery > 0 else '?'}% "
                        f"{float(pack.get('charging_pack_voltage', 0)):.1f}V"
                    )

        if not self.latest_data:
            print("No data received — device may be offline.")

        # Leave the device in its normal cadence. Don't strand it in high-freq
        # mode after a one-shot --status run; that would burn cloud quota if
        # another process checks status often. Cheap no-op for skipped models.
        if self.mqtt_client:
            self._disable_high_freq_reporting()
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
