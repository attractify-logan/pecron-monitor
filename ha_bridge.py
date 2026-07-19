"""
Home Assistant MQTT bridge for pecron-monitor.

Publishes Home Assistant MQTT auto-discovery config and state updates.
"""

import json
import logging
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from helpers import _truthy, _get_kv_single, _fmt_dhm
from energy_state import DEFAULT_MAX_GAP_SECONDS, EnergyIntegrator
from ha_discovery import HomeAssistantDiscoveryMixin
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

log = logging.getLogger("pecron")


class HomeAssistantBridge(HomeAssistantDiscoveryMixin):
    """Publishes Home Assistant MQTT auto-discovery config and state updates."""

    def __init__(self, ha_config: dict, devices: list):
        self.ha_config = ha_config
        self.devices = devices
        self.client = None
        self.discovery_prefix = ha_config.get("discovery_prefix", "homeassistant")
        self._connected = False

        # Retry state for the local MQTT broker (issue #23 follow-up).
        # If the broker is down at startup or drops later, try_reconnect() called
        # from the main loop will attempt a fresh connect every _retry_interval seconds.
        self._last_retry_at = 0.0
        self._retry_interval = ha_config.get("retry_interval", 60)
        # Clear current retained discovery payloads immediately before republishing
        # them. This forces Home Assistant to reload payload field changes on
        # service restart instead of requiring a manual MQTT integration reload.
        self.clear_discovery_on_startup = ha_config.get("clear_discovery_on_startup", True)
        self._clear_current_discovery = False
        # Derived kWh counters are strictly opt-in. Their persistence and sampling
        # state are not touched when disabled, preserving the legacy bridge path.
        self.energy_sensors = bool(_truthy(ha_config.get("energy_sensors", False)))
        self._energy = None
        if self.energy_sensors:
            self._energy = EnergyIntegrator(
                configured_path=ha_config.get("energy_state_path"),
                max_gap_seconds=ha_config.get("energy_max_gap_seconds", DEFAULT_MAX_GAP_SECONDS),
            )

        # Deferred-discovery bookkeeping. Per-port DC-input entities
        # (dc5521, gx16mf1, gx16mf2) are only published the first time the
        # device reports any data for that port, so devices without the
        # hardware don't accumulate ghost Unknown entities in HA.
        # device_key -> dev_info dict (captured during _publish_discovery).
        self._device_dev_info: dict = {}
        # (device_key, port_name) set: ports we've already published discovery
        # for since this bridge connected.
        self._deferred_ports_published: set = set()

        # Cache last-known-good values per device so partial payloads don't zero-out entities
        self._state_cache = {}  # device_key -> dict of last published fields
        # Cache last-known values per device so partial payloads (host-only vs SOC-only)
        # don't clobber sensors to 0/unknown in Home Assistant.
        self._last_state = {}  # device_key -> dict

        # Issue #49: command topics captured during _publish_discovery so the
        # subscribe loop in on_connect stays in lockstep with what's actually
        # registered in HA. Adding a new switch with a command_topic to
        # discovery automatically wires its subscription -- no parallel
        # hardcoded list to drift against.
        self._command_topics: list = []

    def connect(self):
        """Initial connection attempt. If it fails the bridge is not fatal;
        the monitor's main loop will call try_reconnect() periodically."""
        self._last_retry_at = time.time()
        self._connect_attempt()

    def _connect_attempt(self):
        host = self.ha_config.get("mqtt_host", "localhost")
        port = self.ha_config.get("mqtt_port", 1883)
        user = self.ha_config.get("mqtt_user", "")
        pw = self.ha_config.get("mqtt_password", "")

        # Tear down any previous client before a fresh attempt.
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

        client = mqtt.Client(
            client_id="pecron_ha_bridge",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if user:
            client.username_pw_set(user, pw)

        def on_connect(client, ud, flags, rc, props=None):
            if rc == mqtt.CONNACK_ACCEPTED:
                self._connected = True
                log.info("Home Assistant MQTT bridge connected to %s:%d", host, port)
                self._publish_discovery()
                # Issue #49: subscribe to every command_topic that
                # _publish_discovery just registered. Single source of truth --
                # adding a new switch to discovery automatically wires its
                # subscription. Previously the subscribe loop hardcoded
                # ["ac", "dc", "ups"] and silently dropped commands sent to
                # eco_mode, touch_lock, auto_light_flag_as, etc.
                for topic in self._command_topics:
                    client.subscribe(topic, qos=1)

        def on_disconnect(client, ud, disconnect_flags, rc, props=None):
            # paho auto-reconnect handles this after a successful initial connect,
            # but we flip the flag so try_reconnect() is a no-op until paho gives up.
            if self._connected:
                log.warning("Home Assistant MQTT bridge disconnected (rc=%s)", rc)
            self._connected = False

        def on_message(client, ud, msg):
            # Handle HA commands
            parts = msg.topic.split("/")
            if len(parts) == 4 and parts[3] == "set":
                dk = parts[1]
                ctrl = parts[2]
                payload = msg.payload.decode().upper()
                self._handle_command(dk, ctrl, payload)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=self._retry_interval)
        try:
            client.connect(host, port)
            client.loop_start()
            self.client = client
        except (ConnectionRefusedError, OSError) as e:
            log.error(
                "Cannot connect to MQTT broker at %s:%d (%s). Will retry every %ds.",
                host,
                port,
                e,
                self._retry_interval,
            )
            self._connected = False
            self.client = None

    def try_reconnect(self) -> bool:
        """Retry the initial HA MQTT connection if it never succeeded.
        No-op once paho's auto-reconnect is handling an already-established session.
        Returns True when a retry attempt ran (regardless of outcome).
        """
        if self._connected:
            return False
        if self.client is not None:
            # paho is already trying in the background; don't fight it.
            return False
        now = time.time()
        if now - self._last_retry_at < self._retry_interval:
            return False
        self._last_retry_at = now
        log.info("Retrying Home Assistant MQTT connection...")
        self._connect_attempt()
        return True

    def _handle_command(self, device_key: str, control: str, payload: str):
        """Called when HA sends a command. Delegates to the monitor."""
        # This will be wired up by PecronMonitor
        if hasattr(self, "command_callback"):
            self.command_callback(device_key, control, payload)

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

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
