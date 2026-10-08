"""Home Assistant MQTT state formatting and publishing."""

import json

from constants import (
    SENSOR_FIELDS,
    DEVICE_STATUS_LABELS,
    FAULT_ALARM_LABELS,
    WB_CHARGE_VOLTAGE_LABELS,
    WB_DISCHARGE_VOLTAGE_LABELS,
    WB_CHARGE_CURRENT_LABELS,
    WB_DISCHARGE_CURRENT_LABELS,
    WB_HEATING_MODE_LABELS,
    PACK_STATUS_LABELS,
)
from helpers import _fmt_dhm, _get_kv_single, _truthy


class HomeAssistantStateMixin:
    """Formats and publishes Home Assistant state updates."""

    def publish_state(self, device_key: str, kv: dict):
        """Publish current state to HA.

        The device sends multiple payload "shapes" (e.g., host packet vs overall packet).
        Some shapes omit fields and/or carry placeholder zeros; without caching, HA entities
        will flap between valid values and 0/unknown. We therefore merge updates into a
        per-device cache and only overwrite fields when the source field is present.
        """
        if not self._connected:
            return

        cache = self._state_cache.setdefault(device_key, {})

        def _get_first_present(paths):
            """
            Return (present, value) for the first path that exists in this payload shape.
            'present' means the field path resolved to a non-None value (0 is valid).
            """
            for p in paths:
                val = _get_kv_single(kv, p)
                if val is not None:
                    return True, val
            return False, None

        def _get_first_observed(paths):
            """Return a source path even when its observed value is unavailable."""
            unavailable_observed = False
            for path in paths:
                value = kv
                for part in path:
                    if not isinstance(value, dict) or part not in value:
                        break
                    value = value[part]
                else:
                    if value is not None:
                        return True, value
                    unavailable_observed = True
            return unavailable_observed, None

        # Identify payload shape (host packet vs overall packet)
        host_dict = kv.get("host_packet_data_jdb")
        packet_has_host = isinstance(host_dict, dict) and bool(host_dict)

        # ---- Core sensors ----
        # For these, only overwrite when their source field exists in the payload shape.
        # Accept 0 as a real reading *only if the source path is present*.

        # Voltage is special: 0 is never a legitimate reading on a live battery
        # pack (the bus voltage is always >0 while the device can respond at all).
        # A 0.0V value in the packet means the packet was a settings-only shape
        # that carried a placeholder, not that voltage actually dropped to zero.
        # Skip the update in that case; HA graphs stop showing spurious dips,
        # the cached last-known-good value stays visible, and real readings
        # always overwrite it as soon as they arrive. Issue #36.
        present, v = _get_first_present(SENSOR_FIELDS["voltage"])
        if present:
            try:
                new_voltage = round(float(v), 1)
                if new_voltage > 0:
                    cache["voltage"] = new_voltage
                # else: keep the cached value if any; leave cache untouched
                # otherwise so HA shows Unknown until a real reading arrives.
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["temperature"])
        if present:
            try:
                cache["temperature"] = int(float(v))
            except (TypeError, ValueError):
                pass

        # E3800-specific temperature sensors
        present, v = _get_first_present(SENSOR_FIELDS["battery_temp"])
        if present:
            try:
                cache["battery_temp"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["charging_plate_temp"])
        if present:
            try:
                cache["charging_plate_temp"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["inverter_temp"])
        if present:
            try:
                cache["inverter_temp"] = int(float(v))
            except (TypeError, ValueError):
                pass

        # Track whether the top-level total fields were present AND accepted
        # in *this* packet (vs only carried over from the cache). The aggregate
        # fallback at the end of publish_state needs this so it can preserve a
        # canonical top-level reading received in this packet on standalone PPS
        # devices, while still re-aggregating when the top-level was absent or
        # was suppressed by the host-zero guard. See issue #48.
        total_input_top_level_used = False
        total_output_top_level_used = False

        present, v = _get_first_present(SENSOR_FIELDS["total_input_power"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["total_input_power"] = int(float(v))
                total_input_top_level_used = True
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["total_output_power"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["total_output_power"] = int(float(v))
                total_output_top_level_used = True
            except (TypeError, ValueError):
                pass

        v = kv.get("total_energy")
        if v is not None:
            try:
                cache["total_energy"] = round(float(v), 3)
            except (TypeError, ValueError):
                pass

        # AC and DC input power (separate sensors for E3800 and others)
        # ALWAYS publish input power values (including 0) — 0W is valid, "Unknown" is not
        present, v = _get_first_present(SENSOR_FIELDS["ac_input_power"])
        if present:
            try:
                cache["ac_input_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["dc_input_power"])
        if present:
            try:
                cache["dc_input_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["remain_time"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["remain_minutes"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["remain_charging_time"])
        if present and (not packet_has_host or float(v) != 0.0):
            try:
                cache["remain_charging_minutes"] = int(float(v))
            except (TypeError, ValueError):
                pass

        # Human-friendly remaining time for UI
        cache["remain_hm"] = _fmt_dhm(cache.get("remain_minutes"))
        cache["remain_charging_hm"] = _fmt_dhm(cache.get("remain_charging_minutes"))

        # ---- Switch states ----
        # Some payloads don't include these; cache last known.
        def _update_switch(field_key, out_key):
            present, v = _get_first_present(SENSOR_FIELDS[field_key])
            if present:
                cache[out_key] = "ON" if _truthy(v) else "OFF"

        _update_switch("ac_switch", "ac_switch")
        _update_switch("dc_switch", "dc_switch")
        _update_switch("ups_mode", "ups_mode")
        _update_switch("add_bat_status_hm", "add_bat_status_hm")

        # ---- E3800 automation controls ----
        for field in (
            "eco_quite_mode_as",
            "device_touch_locking_as",
            "bypass_enable",
            "auto_light_flag_as",
        ):
            v = kv.get(field)
            if v is not None:
                cache[field] = "ON" if _truthy(v) else "OFF"

        for field in (
            "ac_charging_power_ios",
            "ups_start_charge_value_as",
            "device_standy_times_as",
            "machine_screen_light_as",
            "ac_output_voltage_io",
            "ac_output_frequency_io",
            "noastime_io",
        ):
            v = kv.get(field)
            if v is not None:
                cache[field] = v

        # ---- WB12200 battery management (decode enum indices to friendly labels) ----
        _wb_enum_fields = {
            "charging_limit_voltage": WB_CHARGE_VOLTAGE_LABELS,
            "discharge_limiting_voltage": WB_DISCHARGE_VOLTAGE_LABELS,
            "charging_current_limit": WB_CHARGE_CURRENT_LABELS,
            "discharge_limiting_current": WB_DISCHARGE_CURRENT_LABELS,
            "battery_heating_mode": WB_HEATING_MODE_LABELS,
        }
        for field, labels in _wb_enum_fields.items():
            v = kv.get(field)
            if v is not None:
                try:
                    cache[field] = labels.get(int(v), str(v))
                except (TypeError, ValueError):
                    cache[field] = str(v)

        v = kv.get("FAULT_ALARM_ENUM")
        if v is not None:
            try:
                cache["FAULT_ALARM_ENUM"] = FAULT_ALARM_LABELS.get(int(v), f"Fault {v}")
            except (TypeError, ValueError):
                cache["FAULT_ALARM_ENUM"] = str(v)

        for field in ("beep_voice_us", "battery_indicator_us"):
            v = kv.get(field)
            if v is not None:
                cache[field] = "ON" if _truthy(v) else "OFF"

        # AC output sensors
        present, v = _get_first_present(SENSOR_FIELDS["ac_output_power"])
        if present:
            try:
                cache["ac_output_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["ac_output_voltage"])
        if present:
            try:
                cache["ac_output_voltage"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["dc_output_power"])
        if present:
            try:
                cache["dc_output_power"] = int(float(v))
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["device_status_hm"])
        if present:
            try:
                cache["device_status_hm"] = DEVICE_STATUS_LABELS.get(int(v), str(v))
            except (TypeError, ValueError):
                cache["device_status_hm"] = str(v)

        # Issue #45: device_status_hm is carried only by overall-shape packets.
        # On standalone PPS (E1500LFP) that primarily emit host-shape packets
        # during active operation, the cache freezes at the last overall
        # reading — typically "Shut Down" from before the device woke up. HA
        # then reports the device as Shut Down even while it's actively
        # charging or discharging. Detect the contradiction and infer the
        # live status from observed power activity. The next genuine overall
        # packet still wins (we only override when cached == "Shut Down"
        # AND power is flowing). Sibling pattern of the soc_percent fix in
        # PR #44; this field has no host-shape equivalent to mirror, so we
        # derive it from total_input_power / total_output_power /
        # ac_output_power / dc_output_power which host-shape packets DO carry.
        if cache.get("device_status_hm") == "Shut Down":
            total_in = cache.get("total_input_power") or 0
            total_out = cache.get("total_output_power") or 0
            ac_out = cache.get("ac_output_power") or 0
            dc_out = cache.get("dc_output_power") or 0
            if total_in > 0:
                # Power coming in -> charging. Charging takes precedence over
                # simultaneous discharge (passthrough mode reads as charging
                # to HA, which is the user-relevant state).
                cache["device_status_hm"] = "Charging"
            elif total_out > 0:
                # Power going out and none coming in -> discharging. Pick
                # AC vs DC by which output dominates; if neither dominates,
                # leave the cached value alone rather than guess.
                if ac_out > dc_out:
                    cache["device_status_hm"] = "AC Discharge"
                elif dc_out > 0:
                    cache["device_status_hm"] = "DC Discharge"

        # Current (amps)
        present, v = _get_first_present(SENSOR_FIELDS["current"])
        if present:
            try:
                cache["current"] = round(float(v), 2)
            except (TypeError, ValueError):
                pass

        # ---- Per-port DC input sensors (solar + barrel) ----
        # For idle ports the device truthfully reports 0V / 0A / 0W; publish
        # those zeros through. That's distinct from the pack case above where
        # disconnected slots bleed misleading host-pack data into slot 0.
        # A real zero is honest; a null/Unknown there would be worse UX.
        #
        # Discovery for each port is deferred (see _ensure_port_discovery):
        # a port's three HA entities are only registered the first time the
        # device reports any data for that port. Models without the hardware
        # (E1500LFP) never trigger discovery and therefore don't accumulate
        # ghost Unknown entities.
        for port in ("dc5521", "gx16mf1", "gx16mf2"):
            port_reported = False
            for field_suffix, rounding in [("voltage", 1), ("current", 2), ("power", 0)]:
                field = f"{port}_input_{field_suffix}"
                present, v = _get_first_present(SENSOR_FIELDS[field])
                if present:
                    port_reported = True
                    try:
                        cache[field] = round(float(v), rounding) if rounding else int(float(v))
                    except (TypeError, ValueError):
                        pass
            if port_reported:
                self._ensure_port_discovery(device_key, port)

        # ---- AC output actual readings ----
        present, v = _get_first_present(SENSOR_FIELDS["ac_output_hz"])
        if present:
            try:
                cache["ac_output_hz"] = round(float(v), 1)
            except (TypeError, ValueError):
                pass

        present, v = _get_first_present(SENSOR_FIELDS["ac_output_pf"])
        if present:
            try:
                cache["ac_output_pf"] = round(float(v), 2)
            except (TypeError, ValueError):
                pass

        # ---- Per-pack sensors (charging_pack_data_jdb) ----
        # Pack status enum: 0=no charge, 1=cascade charging, 2=balance no charge,
        # 3=balanced charging, 4=no connection. Slots reporting "no connection"
        # are unoccupied expansion-pack bays on standalone PPS. Publishing their
        # zeroed battery/voltage/current/temp values pollutes HA with ghost
        # entities that can never have real data; skip them so the state JSON
        # omits those keys and HA's template returns None (Unknown).
        packs = kv.get("charging_pack_data_jdb", [])
        if isinstance(packs, list):
            for i, pack in enumerate(packs[:4]):
                if not isinstance(pack, dict):
                    continue
                try:
                    status_val = int(float(pack.get("charging_pack_status", 4)))
                except (TypeError, ValueError):
                    status_val = 4

                if status_val == 4:
                    # Unoccupied slot. Publish explicit JSON null rather than
                    # omitting the keys: HA's value_template returns Undefined
                    # for missing keys and keeps the last known state, which
                    # leaves stale values visible after a reload. null /
                    # Jinja None transitions the entity cleanly to Unknown.
                    for suffix in ("status", "battery", "voltage", "current", "temp"):
                        cache[f"pack_{i}_{suffix}"] = None
                    continue

                cache[f"pack_{i}_status"] = PACK_STATUS_LABELS.get(status_val, str(status_val))

                try:
                    bat = int(float(pack.get("charging_pack_battery", 0)))
                    # Apply same swap fix: if battery=0 and status looks like a percentage
                    if bat == 0 and 5 <= status_val <= 100:
                        bat = status_val
                    cache[f"pack_{i}_battery"] = bat
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_voltage"] = round(
                        float(pack.get("charging_pack_voltage", 0)), 1
                    )
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_current"] = round(
                        float(pack.get("charging_pack_current", 0)), 2
                    )
                except (TypeError, ValueError):
                    pass

                try:
                    cache[f"pack_{i}_temp"] = int(float(pack.get("charging_pack_temp", 0)))
                except (TypeError, ValueError):
                    pass

        # Fallback aggregate: on some models (E3800LFP, E1500LFP) the top-level
        # total_input_power / total_output_power fields are never populated in
        # the MQTT packets, but the per-source ac_input_power / dc_input_power
        # (and ac_output_power / dc_output_power) always are. Compute the total
        # from components when the top-level total isn't in cache so HA's Input
        # Power / Output Power entities don't sit at Unknown indefinitely.
        # Parallels the same fallback already done in monitor._process_data for
        # the status log. Runs AFTER all components are cached so it sees
        # whatever landed in this or any earlier packet.
        #
        # Issue #48: on standalone PPS (no occupied expansion packs) the same
        # "fill once, never refresh" pattern that #43 hit for soc_percent also
        # hits the totals. The host-zero guard above on total_input_power /
        # total_output_power skips the 0 reading on host packets, so when AC
        # is unplugged the cached value stays non-zero (e.g. 1329W) forever
        # while ac_input_power and dc_input_power correctly read 0; the old
        # `if total is None` fallback never re-runs because the cache is
        # already populated. Detect standalone the same way #44 did (no
        # pack_*_status set) and re-aggregate from components for those
        # devices when the top-level total was NOT explicitly accepted from
        # this packet (i.e. either absent or suppressed by the host-zero
        # guard). If the device DID send a real top-level total this packet,
        # respect it -- some models include components other than ac+dc in
        # their top-level reading. Devices with packs preserve the original
        # "fill once, never refresh" behavior unconditionally.
        is_standalone = not any(cache.get(f"pack_{i}_status") for i in range(4))

        ac_in = cache.get("ac_input_power")
        dc_in = cache.get("dc_input_power")
        should_aggregate_input = cache.get("total_input_power") is None or (
            is_standalone and not total_input_top_level_used
        )
        if should_aggregate_input and ac_in is not None and dc_in is not None:
            cache["total_input_power"] = int(ac_in + dc_in)

        ac_out = cache.get("ac_output_power")
        dc_out = cache.get("dc_output_power")
        should_aggregate_output = cache.get("total_output_power") is None or (
            is_standalone and not total_output_top_level_used
        )
        if should_aggregate_output and ac_out is not None and dc_out is not None:
            cache["total_output_power"] = int(ac_out + dc_out)

        # ---- SOC vs Host % ----
        # Your device alternates two payload shapes:
        #   * host packet (has host_packet_data_jdb.*) -> host %
        #   * overall packet (no host_packet_data_jdb) -> overall SOC %
        #
        # IMPORTANT: when host_packet_data_jdb is present, battery_percentage mirrors host %,
        # so we *must not* treat it as SOC in that shape.
        if packet_has_host:
            present, v = _get_first_present(
                [("host_packet_data_jdb", "host_packet_electric_percentage")]
            )
            if present:
                try:
                    cache["host_percent"] = int(float(v))
                except (TypeError, ValueError):
                    pass
        else:
            present, v = _get_first_present([("battery_percentage",)])
            if present:
                try:
                    cache["soc_percent"] = int(float(v))
                except (TypeError, ValueError):
                    pass

        # SOC fallback: on standalone PPS (no occupied expansion packs) the
        # overall SOC and host % are by definition the same number, so mirror
        # host_percent into soc_percent on every host-shape packet so HA tracks
        # live state. Without this, a single "overall" packet that happened to
        # arrive at a stale value (e.g. 100% reported just before the device
        # went into shutdown) leaves soc_percent frozen at that value forever
        # while host_percent updates live with every host-shape packet.
        # Devices WITH expansion packs preserve the original "fill once, don't
        # clobber" behavior: their overall SOC and host % can legitimately
        # differ, and the explicit overall-shape reading is canonical.
        # Pack processing runs earlier in this function, so pack_*_status in
        # cache already reflects this packet's pack state when we get here.
        has_pack = any(cache.get(f"pack_{i}_status") for i in range(4))
        if cache.get("host_percent") is not None and (
            not has_pack or cache.get("soc_percent") is None
        ):
            cache["soc_percent"] = cache["host_percent"]

        # Ensure keys exist for HA templates (but don't force unknown -> 0)
        cache.setdefault("host_percent", None)
        cache.setdefault("soc_percent", None)
        cache.setdefault("remain_hm", _fmt_dhm(cache.get("remain_minutes")))
        cache.setdefault("remain_charging_hm", _fmt_dhm(cache.get("remain_charging_minutes")))

        if self._energy is not None:
            readings = {}
            for channel in ("ac_input", "ac_output", "dc_output"):
                observed, value = _get_first_observed(SENSOR_FIELDS[f"{channel}_power"])
                if observed:
                    readings[channel] = value
            for channel, total in self._energy.update(device_key, readings).items():
                cache[f"{channel}_energy"] = round(total, 9)

        self.client.publish(f"pecron/{device_key}/state", json.dumps(cache), qos=1, retain=True)
