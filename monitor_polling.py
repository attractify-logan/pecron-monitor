"""Local polling and lifecycle behavior for :class:`PecronMonitor`."""

import logging
import time
from typing import Optional

from cloud_api import get_auth_key, get_device_properties_rest
from constants import (
    DEFAULT_CONTROLS,
    LOCAL_READ_TIMEOUT_DEFAULT,
    LOCAL_READ_TIMEOUT_OVERRIDES,
    MODEL_BEHAVIOR,
)
from protocol import build_ttlv_read

try:
    from local_transport import LocalTransport

    HAS_LOCAL = True
except ImportError:
    HAS_LOCAL = False

try:
    from ble_transport import BLETransport, HAS_BLE
except ImportError:
    HAS_BLE = False

log = logging.getLogger("pecron")


class MonitorPollingMixin:
    def _setup_local_transports(self):
        """Set up local TCP and BLE transports for devices with lan_ip/ble in config."""
        configured = {d.get("device_key"): d for d in self.config.get("devices", [])}

        # Auto-discovery: find devices on LAN if they have auth_key but missing/unreliable lan_ip
        devices_to_discover = []
        for dk, cfg in configured.items():
            if cfg.get("auth_key") and (not cfg.get("lan_ip") or cfg.get("auto_discover", False)):
                devices_to_discover.append({"device_key": dk, "auth_key": cfg["auth_key"]})

        if devices_to_discover and HAS_LOCAL:
            try:
                from lan_scan import discover_devices

                discovered = discover_devices(devices_to_discover, timeout=0.5)
                # Update configured IPs with discovered ones
                for dk, ip in discovered.items():
                    if dk in configured:
                        old_ip = configured[dk].get("lan_ip")
                        configured[dk]["lan_ip"] = ip
                        if old_ip != ip:
                            log.info("Updated %s IP: %s → %s", dk, old_ip or "(none)", ip)
            except Exception as e:
                log.warning("Auto-discovery failed: %s", e)

        if HAS_LOCAL:
            for device in self.devices:
                dk = device["device_key"]
                if dk in self.local_transports:
                    continue  # Already set up
                cfg = configured.get(dk, {})
                lan_ip = cfg.get("lan_ip")
                if not lan_ip:
                    continue
                try:
                    auth_key = cfg.get("auth_key")
                    if not auth_key:
                        if self.token_data:
                            log.info("Fetching auth key for %s...", dk)
                            auth_key = get_auth_key(
                                self.token_data["token"], self.region, device["product_key"], dk
                            )
                            log.info(
                                "Got auth key for %s (cache it in config.yaml as auth_key)", dk
                            )
                        else:
                            log.warning("No auth key for %s and no cloud token to fetch one", dk)
                            continue
                    self.local_transports[dk] = LocalTransport(
                        lan_ip,
                        auth_key,
                        device_key=dk,
                        controls=device.get("controls", DEFAULT_CONTROLS),
                        multi_packet_timeout=self._local_read_timeout(device),
                    )
                    log.info("Local transport configured for %s @ %s", dk, lan_ip)
                except Exception as e:
                    log.warning("Failed to set up local transport for %s: %s", dk, e)

        if not self.no_ble and HAS_BLE:
            for device in self.devices:
                dk = device["device_key"]
                if dk in self.ble_transports:
                    continue
                cfg = configured.get(dk, {})
                if cfg.get("ble") is False:
                    continue
                ble_addr = cfg.get("ble_address")
                ble_enabled = cfg.get("ble", False)
                if not ble_addr and not ble_enabled:
                    continue
                try:
                    auth_key = cfg.get("auth_key")
                    if not auth_key and dk in self.local_transports:
                        auth_key = self.local_transports[dk].auth_key_b64
                    if not auth_key and self.token_data:
                        log.info("Fetching auth key for %s (BLE)...", dk)
                        auth_key = get_auth_key(
                            self.token_data["token"], self.region, device["product_key"], dk
                        )
                    if auth_key:
                        self.ble_transports[dk] = BLETransport(
                            auth_key,
                            device_address=ble_addr,
                            device_key=dk,
                            controls=device.get("controls", DEFAULT_CONTROLS),
                        )
                        log.info(
                            "BLE transport configured for %s%s",
                            dk,
                            f" @ {ble_addr}" if ble_addr else " (will scan)",
                        )
                except Exception as e:
                    log.warning("Failed to set up BLE transport for %s: %s", dk, e)

    def _rediscover_device(self, device_key: str) -> bool:
        """Re-discover a single device on the LAN (triggered on connection failure).

        Args:
            device_key: Device key to re-discover

        Returns:
            True if device was found at a new IP, False otherwise
        """
        if not HAS_LOCAL:
            return False

        configured = {d.get("device_key"): d for d in self.config.get("devices", [])}
        cfg = configured.get(device_key)
        if not cfg or not cfg.get("auth_key"):
            return False

        # Skip re-discovery if lan_ip is explicitly configured (pinned IP)
        if cfg.get("lan_ip"):
            log.debug("Skipping re-discovery for %s (lan_ip is pinned in config)", device_key)
            return False

        log.info("Re-discovering device %s (connection lost)...", device_key)
        try:
            from lan_scan import discover_devices

            discovered = discover_devices(
                [{"device_key": device_key, "auth_key": cfg["auth_key"]}], timeout=0.5
            )

            if device_key in discovered:
                new_ip = discovered[device_key]
                old_ip = cfg.get("lan_ip")
                if new_ip != old_ip:
                    log.info(
                        "✅ Re-discovered %s at new IP: %s → %s",
                        device_key,
                        old_ip or "(none)",
                        new_ip,
                    )
                    # Update config and transport
                    cfg["lan_ip"] = new_ip
                    # Re-create transport with new IP
                    from local_transport import LocalTransport

                    rediscovered_device = self._find_device(device_key)
                    self.local_transports[device_key] = LocalTransport(
                        new_ip,
                        cfg["auth_key"],
                        device_key=device_key,
                        controls=rediscovered_device.get("controls", DEFAULT_CONTROLS),
                        multi_packet_timeout=self._local_read_timeout(rediscovered_device),
                    )
                    return True
                else:
                    log.debug("Device %s still at same IP %s", device_key, new_ip)
                    return False
            else:
                log.warning("Could not re-discover device %s", device_key)
                return False
        except Exception as e:
            log.warning("Re-discovery failed for %s: %s", device_key, e)
            return False

    def _connect_local(self, device_key: str) -> bool:
        """Try to connect local transport for a device.

        The Pecron device closes the TCP socket after each response,
        so we reconnect fresh before every read — this is normal behavior.
        """
        lt = self.local_transports.get(device_key)
        if not lt:
            return False

        # E3800LFP connection cooldown: prevent hammering device during lockout
        # Only skip if the PREVIOUS attempt FAILED (not on every attempt)
        now = time.time()
        last_attempt = self._last_connect_attempt.get(device_key, 0)
        failure_count = self._local_connect_failures.get(device_key, 0)

        # Only apply cooldown if we had a recent failure
        if failure_count > 0 and now - last_attempt < 1.0:
            # Skip connection attempt if we failed less than 1 second ago
            log.debug(
                "Skipping connect for %s (cooldown: %.1fs since last failure)",
                device_key,
                now - last_attempt,
            )
            return False

        self._last_connect_attempt[device_key] = now

        try:
            connected = lt.connect()
            if connected:
                # Reset failure counter on successful connection
                self._local_connect_failures[device_key] = 0
            else:
                # Increment failure counter
                self._local_connect_failures[device_key] = (
                    self._local_connect_failures.get(device_key, 0) + 1
                )
            return connected
        except Exception as e:
            log.debug("Local connect failed for %s: %s", device_key, e)
            # Increment failure counter on exception
            self._local_connect_failures[device_key] = (
                self._local_connect_failures.get(device_key, 0) + 1
            )
            return False

    def _channel_id(self, device: dict) -> str:
        return f"qd{device['product_key']}{device['device_key']}"

    def _has_telemetry_fields(self, kv: dict) -> bool:
        """Check if data dict contains COMPLETE telemetry fields (not just settings).

        E3600/E3800 local TCP returns ONLY settings fields (14 fields like
        ac_output_voltage_io, ac_output_frequency_io, noastime_io, ac_switch_hm, etc.)
        but NO telemetry (battery_percentage, voltage, power, temperature).

        E3800 might return battery_percentage alone, but without voltage/power/temp,
        so we need to check for host_packet_data_jdb which contains the real telemetry.

        This method checks for key telemetry fields to determine if local data
        should be treated as primary or if we need to rely on MQTT cloud data.

        Args:
            kv: Data dict to check

        Returns:
            True if data contains COMPLETE telemetry fields, False if only settings
        """
        # host_packet_data_jdb is the most reliable indicator - it contains
        # voltage, temperature, and battery % in nested form
        # E1500LFP returns this with full data, E3600/E3800 do not
        if "host_packet_data_jdb" in kv:
            host_data = kv["host_packet_data_jdb"]
            if isinstance(host_data, dict) and host_data:
                # Check if it has actual voltage/temp data (not just empty dict)
                try:
                    has_voltage = float(host_data.get("host_packet_voltage", 0)) > 0
                except (ValueError, TypeError):
                    has_voltage = False
                has_temp = "host_packet_temp" in host_data
                if has_voltage or has_temp:
                    return True

        # Check for power data structures (E1500 has these, E3600/E3800 don't via local TCP)
        power_structures = [
            "ac_data_output_hm",
            "dc_data_output_hm",
            "ac_data_input_hm",
            "dc_data_input_hm",
        ]

        for field in power_structures:
            if field in kv:
                value = kv[field]
                if isinstance(value, dict) and value:
                    return True

        # Check for top-level power fields
        if kv.get("total_input_power", 0) > 0 or kv.get("total_output_power", 0) > 0:
            return True

        # battery_percentage alone is NOT enough (issue #84): E3600/E3800
        # settings-only payloads also include battery_percentage, so on its
        # own it can't distinguish "complete telemetry" from "just settings".
        # Require it alongside a flat temperature field (battery_temp etc.,
        # used by E300LFP/E3800-style flat payloads per SENSOR_FIELDS) as
        # corroborating evidence that a real telemetry packet arrived.
        battery_pct = kv.get("battery_percentage")
        has_flat_temp = any(
            field in kv for field in ("battery_temp", "charging_plate_temp", "inverter_temp")
        )
        if battery_pct is not None and has_flat_temp:
            try:
                if int(float(battery_pct)) >= 0:
                    return True
            except (ValueError, TypeError):
                pass

        return False

    def _find_device(self, device_key: str) -> dict:
        for d in self.devices:
            if d["device_key"] == device_key:
                return d
        return {}

    def _request_status(self, device_keys: Optional[set[str]] = None):
        # Clear local data tracking only for devices included in this request.
        # Targeted within-cycle retries must not discard other devices' source state.
        if device_keys is None:
            self._local_data_keys.clear()
        else:
            self._local_data_keys.difference_update(device_keys)

        for device in self.devices:
            if device_keys is not None and device["device_key"] not in device_keys:
                continue
            dk = device["device_key"]

            # Priority: BLE → TCP/WiFi → Cloud MQTT → REST API

            # Try BLE first (no infrastructure needed)
            ble = self.ble_transports.get(dk)
            if ble:
                if not ble.connected:
                    try:
                        ble.connect()
                    except Exception as e:
                        log.debug("BLE connect failed for %s: %s", dk, e)
                if ble.connected:
                    try:
                        kv = ble.read_status()
                        if kv:
                            log.debug("Got status via BLE for %s", dk)
                            self._merge_device_data(dk, kv)

                            # Only mark as local data if it contains telemetry fields
                            has_telemetry = self._has_telemetry_fields(kv)
                            if has_telemetry:
                                self._local_data_keys.add(dk)  # Mark as local data
                                log.debug("BLE data contains telemetry for %s", dk)
                            else:
                                log.debug(
                                    "BLE data is settings-only for %s (telemetry from cloud)", dk
                                )

                            self._process_data(dk, kv, source="BLE")
                            continue
                    except Exception as e:
                        log.warning("BLE read failed for %s: %s", dk, e)

            # Try TCP/WiFi local transport
            # Pecron devices close TCP after each response, so always reconnect
            lt = self.local_transports.get(dk)
            if lt:
                connected = self._connect_local(dk)
                if not connected:
                    # Connection failed — check if we should trigger re-discovery
                    failure_count = self._local_connect_failures.get(dk, 0)
                    configured = {d.get("device_key"): d for d in self.config.get("devices", [])}
                    cfg = configured.get(dk)
                    has_pinned_ip = cfg and cfg.get("lan_ip")

                    if has_pinned_ip:
                        # Pinned IP: skip re-discovery, fall through to cloud MQTT
                        log.debug(
                            "Local TCP connection failed for %s (pinned IP, failure #%d) — skipping to cloud",
                            dk,
                            failure_count,
                        )
                    elif failure_count >= 5:
                        # Auto-discovered device with 5+ failures: try re-discovery
                        log.debug(
                            "Local TCP connection failed for %s (%d consecutive failures), attempting re-discovery...",
                            dk,
                            failure_count,
                        )
                        if self._rediscover_device(dk):
                            # Re-discovered at new IP, try connecting again
                            lt = self.local_transports.get(dk)  # Get updated transport
                            if lt:
                                connected = self._connect_local(dk)
                    else:
                        # Auto-discovered device with <5 failures: skip to cloud
                        log.debug(
                            "Local TCP connection failed for %s (auto-discovered, failure #%d) — skipping to cloud",
                            dk,
                            failure_count,
                        )

                if lt.connected:
                    try:
                        kv = lt.read_status()
                        if kv:
                            log.debug("Got status via LOCAL TCP for %s", dk)
                            self._merge_device_data(dk, kv)

                            # Only mark as local data if it contains telemetry fields
                            # E3600/E3800 local TCP returns ONLY settings (14 fields), no telemetry
                            # We need to let MQTT cloud data be the primary source for these devices
                            has_telemetry = self._has_telemetry_fields(kv)
                            if has_telemetry:
                                self._local_data_keys.add(dk)  # Mark as local data
                                log.debug("Local TCP data contains telemetry for %s", dk)
                            else:
                                log.debug(
                                    "Local TCP data is settings-only for %s (telemetry from cloud)",
                                    dk,
                                )

                            self._process_data(dk, kv, source="LOCAL TCP")
                            # Reset failure counter on successful read
                            self._local_connect_failures[dk] = 0
                            # DON'T continue — still need to publish MQTT read request
                            # E3600/E3800 local TCP only returns settings, need cloud for telemetry
                    except Exception as e:
                        log.warning("Local TCP read failed for %s: %s", dk, e)

            # Always publish MQTT read request (even if local TCP connected)
            # E3600/E3800 local TCP only returns settings — we NEED cloud MQTT for telemetry
            if self.mqtt_client:
                cid = self._channel_id(device)
                pkt = build_ttlv_read(self._next_packet_id())
                topic = f"q/1/d/{cid}/bus"
                result = self.mqtt_client.publish(topic, pkt, qos=1)
                log.debug("Published TTLV read to %s (rc=%s, mid=%s)", topic, result.rc, result.mid)

            # If we haven't received MQTT data for this device yet, try REST API.
            # In rest_only mode there is no MQTT or local TCP, so we must re-fetch
            # every poll rather than only on the first time (fix from @brucehoult
            # in issue #14; otherwise --rest-only stops updating after cycle 1).
            if self.rest_only or dk not in self.latest_data:
                if self.token_data:  # Only available if not in offline mode
                    log.debug(
                        "Fetching status via REST API for %s (rest_only=%s, cached=%s)...",
                        dk,
                        self.rest_only,
                        dk in self.latest_data,
                    )
                    kv = get_device_properties_rest(
                        self.token_data["token"], self.region, device["product_key"], dk
                    )
                    if kv:
                        log.info("Got status via REST API for %s", dk)
                        self._merge_device_data(dk, kv)
                        self._process_data(dk, kv, source="REST API")

    def _continuous_local_retry_device_keys(self) -> set[str]:
        """Return local multi-packet devices eligible for within-cycle retries."""
        return {
            device["device_key"]
            for device in self.devices
            if device["device_key"] in self.local_transports
            and (device.get("device_name") or device.get("product_name") or "")
            in LOCAL_READ_TIMEOUT_OVERRIDES
        }

    def _request_status_with_local_retries(self, poll_interval: float) -> float:
        """Request one cycle, retrying incomplete local multi-packet devices.

        Returns the elapsed portion of the poll cycle so ``run`` can wait only
        for the remaining interval rather than stacking a fresh interval after
        retries.
        """
        eligible = self._continuous_local_retry_device_keys()
        cycle_started = time.monotonic()
        self._request_status()
        if not eligible:
            return 0.0

        incomplete = eligible.difference(self._local_data_keys)
        deadline = cycle_started + min(45.0, max(0.0, float(poll_interval)))

        while incomplete and self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            time.sleep(min(10.0, remaining))
            self._request_status(device_keys=incomplete)
            incomplete.difference_update(self._local_data_keys)

        return time.monotonic() - cycle_started

    @staticmethod
    def _high_freq_effective(device: dict) -> bool:
        """Return False for models where high_frequency_reporting is a known no-op
        (issue #14: E3600LFP ignores the setting; don't waste cloud requests)."""
        name = device.get("device_name") or device.get("product_name") or ""
        return MODEL_BEHAVIOR.get(name, {}).get("high_freq_effective", True)

    @staticmethod
    def _local_read_timeout(device: dict) -> float:
        """Per-model inter-packet timeout for local TCP multi-packet reads
        (issue #84: E3600/E3800 need longer gaps than the 3.0s global default)."""
        name = device.get("device_name") or device.get("product_name") or ""
        return LOCAL_READ_TIMEOUT_OVERRIDES.get(name, LOCAL_READ_TIMEOUT_DEFAULT)

    def _enable_high_freq_reporting(self, stagger: float = 0):
        """Enable high-frequency MQTT reporting on all devices for fast cache warm-up.

        Args:
            stagger: Seconds to wait between devices (helps cloud process multi-device requests)
        """
        effective = [d for d in self.devices if self._high_freq_effective(d)]
        skipped = [d for d in self.devices if not self._high_freq_effective(d)]
        for d in skipped:
            log.debug(
                "Skipping high-freq enable for %s (ineffective on this model, issue #14)",
                d.get("device_name") or d["device_key"],
            )
        for i, d in enumerate(effective):
            dk = d["device_key"]
            try:
                # high_frequency_reporting is transient: device auto-reverts the
                # value so a post-write read-back will mismatch and log a noisy
                # but meaningless warning (issue #50). Skip verification here.
                self.send_control(dk, "high_frequency_reporting", 3, verify=False)
                log.info("Enabled high-freq reporting for %s", dk)
            except Exception as e:
                log.debug("Could not enable high-freq for %s: %s", dk, e)
            if stagger > 0 and i < len(effective) - 1:
                time.sleep(stagger)

    def _disable_high_freq_reporting(self):
        """Disable high-frequency reporting after warm-up period."""
        for d in self.devices:
            if not self._high_freq_effective(d):
                continue  # never enabled → nothing to disable
            dk = d["device_key"]
            try:
                # Transient control code; suppress read-back verification (#50).
                self.send_control(dk, "high_frequency_reporting", 0, verify=False)
                log.info("Disabled high-freq reporting for %s (warm-up complete)", dk)
            except Exception as e:
                log.debug("Could not disable high-freq for %s: %s", dk, e)
