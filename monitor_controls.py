"""Control routing, probing, aliases, and Home Assistant command dispatch."""

import logging
import time
from typing import Any

from cloud_api import set_device_property_rest
from constants import DEFAULT_CONTROLS
from protocol import build_ttlv_write_bool, build_ttlv_write_enum


log = logging.getLogger("pecron")


class MonitorControlsMixin:
    def send_control(self, device_key: str, control_code: str, value, verify: bool = True):
        """Send a control command. Auto-detects type from TSL (BOOL, ENUM, INT).

        `verify=True` (default) asks the local/BLE transport to read the data
        point back and confirm the device applied the write (issue #46). Pass
        `verify=False` for transient control codes that the device intentionally
        auto-reverts (e.g. `high_frequency_reporting`, see issue #50) so the
        read-back doesn't spuriously log a mismatch warning. Cloud-only
        transports (MQTT/REST) are unaffected -- they have no read-back step.
        """
        device = self._find_device(device_key)
        if not device:
            log.error("Device %s not found", device_key)
            return False

        controls = device.get("controls", DEFAULT_CONTROLS)
        ctrl = controls.get(control_code)
        if not ctrl:
            log.error("Control %s not found for device %s", control_code, device_key)
            return False

        access = ctrl.get("access", "R").upper()
        if "W" not in access:
            log.error("Control %s is read-only (access=%s)", control_code, access)
            return False

        cid = self._channel_id(device)
        pid = self._next_packet_id()
        ctrl_type = str(ctrl.get("type", "BOOL")).upper()

        if ctrl_type == "BOOL":
            pkt = build_ttlv_write_bool(pid, ctrl["id"], bool(value))
        elif ctrl_type in ("ENUM", "INT", "LONG"):
            pkt = build_ttlv_write_enum(pid, ctrl["id"], int(value))
        else:
            log.warning("Unknown control type '%s' for %s, trying bool", ctrl_type, control_code)
            pkt = build_ttlv_write_bool(pid, ctrl["id"], bool(value))

        # Only attempt local radio writes for boolean switches (hardware limitation)
        if ctrl_type == "BOOL":
            # Try BLE first
            ble = self.ble_transports.get(device_key)
            if ble and ble.connected:
                try:
                    if ble.send_control(ctrl["id"], value, ctrl_type, verify=verify):
                        log.info(
                            "Sent %s=%s (type=%s) to %s via BLE",
                            control_code,
                            value,
                            ctrl_type,
                            device_key,
                        )
                        return True
                except Exception as e:
                    log.warning("BLE control failed: %s", e)

            # Try TCP/WiFi local transport (reconnect if needed - Pecron closes TCP after each exchange)
            lt = self.local_transports.get(device_key)
            if lt:
                if not lt.connected:
                    try:
                        self._connect_local(device_key)
                    except Exception as e:
                        log.debug("Local TCP reconnect failed for %s: %s", device_key, e)
                if lt.connected:
                    try:
                        if lt.send_control(ctrl["id"], value, ctrl_type, verify=verify):
                            log.info(
                                "Sent %s=%s (type=%s) to %s via TCP",
                                control_code,
                                value,
                                ctrl_type,
                                device_key,
                            )
                            return True
                    except Exception as e:
                        log.warning("TCP control failed: %s", e)

        # Fall back to cloud transports
        # Fix: Route non-boolean configurations (ENUM/INT) straight to REST API (issue #84)
        if ctrl_type != "BOOL" and self.token_data:
            if set_device_property_rest(
                self.token_data["token"],
                self.region,
                device["product_key"],
                device_key,
                {control_code: value},
            ):
                log.info("Sent %s=%s to %s via CLOUD REST API", control_code, value, device_key)
                return True

        # Boolean primary cloud transport / last-resort fallback channel
        if self.mqtt_client is not None:
            self.mqtt_client.publish(f"q/1/d/{cid}/bus", pkt, qos=1)
            log.info(
                "Sent %s=%s (type=%s) to %s via CLOUD MQTT",
                control_code,
                value,
                ctrl_type,
                device_key,
            )
            return True

        # Last-resort cloud backup for Boolean toggles if MQTT client instance drops
        if self.token_data:
            if set_device_property_rest(
                self.token_data["token"],
                self.region,
                device["product_key"],
                device_key,
                {control_code: value},
            ):
                log.info("Sent %s=%s to %s via CLOUD REST API", control_code, value, device_key)
                return True

        log.debug("Cannot send control %s: no cloud transport available", control_code)
        return False

    def _extract_value_by_key(self, obj: Any, key: str):
        """Find first occurrence of a key in nested dict/list structures."""
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = self._extract_value_by_key(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._extract_value_by_key(item, key)
                if found is not None:
                    return found
        return None

    def _normalize_probe_readback(self, value):
        """Normalize readback values for robust integer comparison."""
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("on", "true", "enabled"):
                return 1
            if lowered in ("off", "false", "disabled"):
                return 0
            try:
                return int(float(lowered))
            except ValueError:
                return None
        return None

    def probe_control_values(
        self, device_key: str, control_code: str, min_value: int = 0, max_value: int = 255
    ) -> dict:
        """Probe supported control values from min_value upward with set-then-readback validation.

        For each candidate value:
        1) Send control value
        2) Request status
        3) Read back same control key
        4) Continue while readback equals candidate value

        Returns probe details including the contiguous valid value set.
        """
        device = self._find_device(device_key)
        if not device:
            return {
                "device_key": device_key,
                "control_code": control_code,
                "valid_values": [],
                "stop_value": 0,
                "last_readback": None,
                "reason": "device_not_found",
            }

        controls = device.get("controls", DEFAULT_CONTROLS)
        ctrl = controls.get(control_code)
        if not ctrl:
            return {
                "device_key": device_key,
                "control_code": control_code,
                "valid_values": [],
                "stop_value": 0,
                "last_readback": None,
                "reason": "control_not_found",
            }

        valid_values = []
        stop_value = min_value
        last_readback = None
        reason = "readback_mismatch"

        for candidate in range(min_value, max_value + 1):
            stop_value = candidate

            sent = self.send_control(device_key, control_code, candidate)
            if not sent:
                reason = "send_failed"
                break

            # Allow device to apply state before requesting readback.
            time.sleep(3)
            # Clear only this device's cached reading before fresh readback
            self.latest_data.pop(device_key, None)
            self._request_status()
            time.sleep(1)

            kv = self.latest_data.get(device_key, {})
            raw_readback = self._extract_value_by_key(kv, control_code)
            normalized_readback = self._normalize_probe_readback(raw_readback)
            last_readback = raw_readback

            if normalized_readback != candidate:
                reason = "readback_mismatch"
                break

            valid_values.append(candidate)
        else:
            reason = "max_reached"

        return {
            "device_key": device_key,
            "control_code": control_code,
            "valid_values": valid_values,
            "stop_value": stop_value,
            "last_readback": last_readback,
            "reason": reason,
        }

    # Convenience aliases
    def send_bool_control(self, device_key: str, control_code: str, value: bool):
        return self.send_control(device_key, control_code, value)

    def set_ac(self, device_key: str, on: bool):
        return self.send_bool_control(device_key, "ac_switch_hm", on)

    def set_dc(self, device_key: str, on: bool):
        return self.send_bool_control(device_key, "dc_switch_hm", on)

    def set_ups(self, device_key: str, on: bool):
        return self.send_bool_control(device_key, "ups_status_hm", on)

    def _ha_command(self, device_key: str, control: str, payload: Any):
        """Handle commands from Home Assistant."""
        ctrl_map = {
            "ac": "ac_switch_hm",
            "dc": "dc_switch_hm",
            "ups": "ups_status_hm",
            "eco_mode": "eco_quite_mode_as",
            "touch_lock": "device_touch_locking_as",
            "auto_light_flag_as": "auto_light_flag_as",
        }
        code = ctrl_map.get(control)
        if code:
            if isinstance(payload, bool):
                is_on = payload
            else:
                is_on = str(payload).upper() in ("ON", "TRUE", "1")
            self.send_bool_control(device_key, code, is_on)
        elif control == "ac_charging_power":
            val_str = str(payload).strip()
            is_percent = "%" in val_str
            val_str = val_str.replace("%", "")
            try:
                val = int(val_str)
                if is_percent:
                    index = val // 10
                else:
                    index = val // 10 if val > 10 else val

                if 0 <= index <= 10:
                    self.send_control(device_key, "ac_charging_power_ios", index)
            except (ValueError, TypeError):
                log.warning("Invalid AC charging power payload from HA: %r", payload)
        elif control == "ups_charge_threshold":
            val_str = str(payload).replace("%", "").strip()
            try:
                pct = int(val_str)
                if 30 <= pct <= 100:
                    self.send_control(device_key, "ups_start_charge_value_as", pct)
            except (ValueError, TypeError):
                log.warning("Invalid UPS charge threshold payload from HA: %r", payload)
        else:
            log.warning(
                "HA command for unknown control %r on %s -- no slug->TSL mapping (issue #54)",
                control,
                device_key,
            )
