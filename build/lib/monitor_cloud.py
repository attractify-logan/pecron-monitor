"""Cloud authentication, MQTT lifecycle, and monitor run-loop orchestration."""

import json
import logging
import time
from typing import Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from cloud_api import login, resolve_devices
from constants import DEFAULT_CONTROLS


log = logging.getLogger("pecron")

# Cloud polling rate-limit floor.
# Pecron's cloud applies a per-account cap of roughly 1280 polls/day (issue #29,
# verified empirically by @brucehoult: poll_interval=62 trips code 4026 around
# 23:45 UTC, poll_interval=63 makes it through, poll_interval=120 sails through
# clean). The argument for 63 as the hard floor: at poll_interval=60, his
# account ran out of budget at ~23:00 UTC; bumping to 63 stretches the same
# budget to 23 * 63/60 = 24.15 hours, which crosses the 00:00 UTC reset. We
# refuse to start below MIN_POLL_INTERVAL and warn below RECOMMENDED_POLL_INTERVAL.
MIN_POLL_INTERVAL = 63
RECOMMENDED_POLL_INTERVAL = 70


def _validate_poll_interval(poll_interval: int) -> None:
    """Refuse below MIN_POLL_INTERVAL, warn below RECOMMENDED_POLL_INTERVAL."""
    if poll_interval < MIN_POLL_INTERVAL:
        raise ValueError(
            f"poll_interval={poll_interval}s is below the {MIN_POLL_INTERVAL}s floor. "
            f"Pecron's cloud rate-limits per account at roughly 1280 polls/day; "
            f"poll_interval=62 reliably trips code 4026 ('Insufficient resources') "
            f"around 23:45 UTC daily, while {MIN_POLL_INTERVAL}s is the empirical "
            f"minimum that stretches the budget past the 00:00 UTC reset. Raise "
            f"poll_interval to {RECOMMENDED_POLL_INTERVAL} or higher in config.yaml. "
            f"See pecron-monitor issue #29 for the full evidence trail."
        )
    if poll_interval < RECOMMENDED_POLL_INTERVAL:
        log.warning(
            "poll_interval=%ds is below the recommended %ds and may trip cloud rate-limit "
            "code 4026 within ~24h (issue #29). Consider raising to %d.",
            poll_interval,
            RECOMMENDED_POLL_INTERVAL,
            RECOMMENDED_POLL_INTERVAL,
        )


def _validate_poll_interval_for_mode(poll_interval: int, force_offline: bool) -> None:
    """Apply the cloud poll floor only when cloud polling can be used."""
    if force_offline:
        if poll_interval < MIN_POLL_INTERVAL:
            log.info(
                "poll_interval=%ds is below the cloud floor but allowed in local/offline mode",
                poll_interval,
            )
        return
    _validate_poll_interval(poll_interval)


class MonitorCloudMixin:
    def authenticate(self, force_offline: bool = False, skip_local: Optional[bool] = None):
        """Authenticate and set up transports.

        Args:
            force_offline: If True, skip cloud login and use cached config only.
                          Auto-detected when all devices have local credentials.
        """
        # Check if we can run fully offline
        can_offline = self._check_offline_capable()

        # Honor skip_local flag for this monitor instance (only if provided)
        if skip_local is not None:
            self.skip_local_setup = bool(skip_local)

        # If running in REST-only mode, always skip local transports
        if self.rest_only:
            self.skip_local_setup = True

        if force_offline:
            if not can_offline:
                raise RuntimeError(
                    "Cannot run in offline mode: missing required fields.\n"
                    "Each device needs: lan_ip or ble_address, auth_key, product_key, device_key.\n"
                    "Run --setup first to fetch and cache these credentials."
                )
            self.offline_mode = True
            self._fell_back_to_offline = False  # user-requested; no cloud retry
            log.info("🔒 OFFLINE MODE — using cached credentials from config.yaml")
            self._build_devices_from_config()
        elif not force_offline and can_offline:
            # Try cloud first, graceful offline fallback
            try:
                log.info("Logging in to Pecron cloud (%s)...", self.region["name"])
                self.token_data = login(self.config["email"], self.config["password"], self.region)
                log.info("Logged in as %s", self.token_data["uid"])
                log.info("Resolving devices...")
                self.devices = resolve_devices(self.config, self.token_data["token"], self.region)
                if not self.devices:
                    raise RuntimeError("No valid devices found.")
                if not self.skip_local_setup:
                    self._setup_local_transports()
                self.offline_mode = False
                self._fell_back_to_offline = False
            except Exception as e:
                log.warning("Cloud login failed (%s), falling back to offline mode", e)
                self.offline_mode = True
                self._fell_back_to_offline = True  # issue #23: retry periodically
                self._last_cloud_retry_at = time.time()
                self._build_devices_from_config()
        else:
            # Normal cloud-first mode
            log.info("Logging in to Pecron cloud (%s)...", self.region["name"])
            self.token_data = login(self.config["email"], self.config["password"], self.region)
            log.info("Logged in as %s", self.token_data["uid"])
            log.info("Resolving devices...")
            self.devices = resolve_devices(self.config, self.token_data["token"], self.region)
            if not self.devices:
                raise RuntimeError("No valid devices found.")
            if not self.skip_local_setup:
                self._setup_local_transports()

    def _check_offline_capable(self) -> bool:
        """Check if all devices have the required fields for offline operation."""
        configured = self.config.get("devices", [])
        if not configured:
            return False
        for d in configured:
            has_transport = d.get("lan_ip") or d.get("ble_address") or d.get("ble")
            has_auth = d.get("auth_key")
            has_ids = d.get("product_key") and d.get("device_key")
            if not (has_transport and has_auth and has_ids):
                return False
        return True

    def _build_devices_from_config(self):
        """Build device list from config.yaml when running offline."""
        configured = self.config.get("devices", [])
        if not configured:
            raise RuntimeError("No devices in config.yaml")

        for d in configured:
            pk = d["product_key"]
            dk = d["device_key"]
            name = d.get("name", "Unknown")

            # Load cached TSL if available, otherwise use defaults
            controls = d.get("tsl_cache", DEFAULT_CONTROLS)

            self.devices.append(
                {
                    "product_key": pk,
                    "device_key": dk,
                    "device_name": name,
                    "product_name": name,
                    "controls": controls,
                }
            )
            log.info("  📦 Loaded from config: %s (pk=%s, dk=%s)", name, pk, dk)

        log.info("Loaded %d device(s) from config", len(self.devices))

        # Set up local transports (TCP + BLE)
        if self.no_ble:
            log.info("BLE disabled (--no-ble flag)")
        if not self.skip_local_setup:
            self._setup_local_transports()

    # --- MQTT callbacks ---

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != mqtt.CONNACK_ACCEPTED:
            self._mqtt_connect_failures += 1
            log.error("MQTT connection failed: %s", mqtt.connack_string(rc))
            return
        self._mqtt_connect_failures = 0
        log.info("MQTT connected")
        for device in self.devices:
            cid = self._channel_id(device)
            for suffix in ["bus_", "ack_", "onl_"]:
                topic = f"q/2/d/{cid}/{suffix}"
                client.subscribe(topic, qos=1)
                log.debug("  Subscribed: %s", topic)
            log.info(
                "Subscribed to %s (pk=%s, dk=%s, channel=%s)",
                device["device_name"],
                device["product_key"],
                device["device_key"],
                cid,
            )

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("Non-JSON MQTT message on %s (%d bytes)", msg.topic, len(msg.payload))
            return

        topic_suffix = msg.topic.split("/")[-1]
        device_key = payload.get("deviceKey", "")
        log.debug(
            "MQTT message: topic=%s suffix=%s dk=%s keys=%s",
            msg.topic,
            topic_suffix,
            device_key,
            list(payload.keys()),
        )

        if topic_suffix == "bus_" and "data" in payload:
            kv = payload["data"].get("kv", {})
            log.debug("MQTT kv keys for %s: %s", device_key, list(kv.keys()))
            if kv:
                # Always merge MQTT data with existing local data
                # This is essential for E3600/E3800LFP which:
                # 1. Send incomplete local TCP packets (only settings, no telemetry)
                # 2. Send alternating MQTT packets (one with battery%, another with power)
                # We merge all incoming data, then process from the accumulated state
                if device_key in self._local_data_keys:
                    log.debug("Merging CLOUD MQTT data with existing local data for %s", device_key)
                else:
                    log.debug("Processing CLOUD MQTT data for %s", device_key)

                self._merge_device_data(device_key, kv)

                # Process from the ACCUMULATED data, not just this message
                # This ensures we have the complete picture after multiple partial packets
                accumulated = self.latest_data.get(device_key, kv)
                self._process_data(device_key, accumulated, source="CLOUD MQTT")
            else:
                log.debug("bus_ message with empty kv: %s", list(payload["data"].keys()))
        elif topic_suffix == "onl_" and "data" in payload:
            online = payload["data"].get("value", 0) == 1
            if online:
                self._on_device_online(device_key)
            else:
                self._on_device_offline(device_key)
        elif topic_suffix == "ack_":
            log.debug("ACK received for device %s", device_key)
        elif topic_suffix == "sys_":
            # System messages (responses to our publishes, device online/offline events)
            code = payload.get("code")
            msg_text = payload.get("msg", "")
            msg_type = payload.get("type", "")
            if code == 4007:
                if not hasattr(self, "_4007_warned"):
                    self._4007_warned = True
                    log.warning(
                        "Cloud reported 'device is not bound' (code 4007) during startup/control traffic."
                    )
                    log.warning(
                        "This can mean the wrong product_key is configured, but it can also be a transient or noisy cloud-system message."
                    )
                    log.warning(
                        "If device verification succeeds or telemetry still arrives via MQTT/local/REST, you can usually ignore this warning."
                    )
                    log.warning(
                        "Only treat it as actionable if the warning persists AND the device never produces telemetry."
                    )
                    log.warning(
                        "Then run 'python pecron_monitor.py --diagnose -v' or '--setup' to verify the product_key/device_key pair."
                    )
            elif code == 4026:
                log.warning(
                    "Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type
                )
                if not getattr(self, "_4026_warned", False):
                    self._4026_warned = True
                    configured = self.config.get("poll_interval", RECOMMENDED_POLL_INTERVAL)
                    log.error(
                        "Pecron cloud returned code 4026 ('Insufficient resources'). This is a "
                        "per-account polling rate-limit (~1280 polls/day) — not a Pecron-side "
                        "outage. Current poll_interval=%ds. Raise it in config.yaml (>=%d "
                        "recommended) and restart. The cap resets at 00:00 UTC. See issue #29.",
                        configured,
                        RECOMMENDED_POLL_INTERVAL,
                    )
            elif code and code != 200:
                log.warning(
                    "Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type
                )
            else:
                log.debug(
                    "Cloud system message: code=%s msg='%s' type=%s", code, msg_text, msg_type
                )

    def _token_needs_refresh(self) -> bool:
        if self.offline_mode:
            return False
        if not self.token_data:
            return True
        return time.time() > (self.token_data["expires_at"] - 300)

    def _try_cloud_recovery(self) -> bool:
        """Retry cloud login after a prior transient failure (issue #23).

        Only triggers when we're in offline_mode AND the fallback was unplanned
        (not --offline). On success, rejoins cloud MQTT and clears the flag.
        """
        if not self.offline_mode or not self._fell_back_to_offline:
            return False
        interval = self.config.get("cloud_retry_interval", 300)
        now = time.time()
        if now - self._last_cloud_retry_at < interval:
            return False
        self._last_cloud_retry_at = now

        # Phase 1: prove cloud is reachable before mutating any state.
        try:
            log.info("Retrying cloud login (offline recovery)...")
            token = login(self.config["email"], self.config["password"], self.region)
            devices = resolve_devices(self.config, token["token"], self.region)
            if not devices:
                raise RuntimeError("Cloud login succeeded but no devices resolved")
        except Exception as e:
            log.info("Cloud retry failed: %s (next attempt in %ds)", e, interval)
            return False

        # Phase 2: login succeeded. Apply state and rebuild MQTT/local transports.
        self.token_data = token
        self.devices = devices
        self.offline_mode = False
        self._fell_back_to_offline = False
        log.info("Cloud recovered, reconnecting MQTT")
        try:
            if not self.skip_local_setup:
                self._setup_local_transports()
            self.connect_mqtt()
        except Exception as e:
            # A post-login MQTT/local failure shouldn't wedge us back in offline mode,
            # but log loudly so operators see it. paho's auto-reconnect + the HA retry
            # loop will recover on their own.
            log.warning("Cloud recovered but MQTT/local setup hit an error: %s", e)
        return True

    def _recover_mqtt_connection(self) -> bool:
        """Rebuild the MQTT client after broker CONNACK failures."""
        if self.offline_mode or getattr(self, "rest_only", False):
            return False
        if self._mqtt_connect_failures == 0:
            return False

        interval = self.config.get("mqtt_reconnect_interval", 60)
        now = time.time()
        if now - self._last_mqtt_rebuild_at < interval:
            return False
        self._last_mqtt_rebuild_at = now

        failures = self._mqtt_connect_failures
        log.warning("Rebuilding MQTT client after %d connection failure(s)", failures)
        try:
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
        except Exception as e:
            log.debug("Ignoring MQTT disconnect error during rebuild: %s", e)

        self._mqtt_connect_failures = 0
        try:
            self.connect_mqtt()
        except Exception as e:
            self._mqtt_connect_failures = failures
            log.warning("MQTT rebuild failed: %s", e)
            return False
        return True

    # --- MQTT connection ---

    def connect_mqtt(self):
        if self.offline_mode:
            log.info("Offline mode — skipping MQTT connection")
            return

        if getattr(self, "rest_only", False):
            log.info("REST-only mode — skipping MQTT connection")
            return

        client_id = f"qu_{self.token_data['uid']}_{int(time.time() * 1000)}"
        self.mqtt_client = mqtt.Client(
            client_id=client_id,
            transport="websockets",
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self.mqtt_client.ws_set_options(path=self.region["mqtt_path"])
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set(username="", password=self.token_data["token"])
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_message = self._on_message
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
        log.info(
            "Connecting to MQTT broker %s:%d...", self.region["mqtt_host"], self.region["mqtt_port"]
        )
        self.mqtt_client.connect(self.region["mqtt_host"], self.region["mqtt_port"])
        self.mqtt_client.loop_start()

    # --- Main loop ---

    def run(self, enable_ha=False, force_offline=False):
        self._running = True

        # Fail fast on a broken cloud poll_interval before any cloud login or
        # MQTT connection. Local/offline mode is not subject to the cloud quota.
        poll_interval = self.config.get("poll_interval", RECOMMENDED_POLL_INTERVAL)
        _validate_poll_interval_for_mode(poll_interval, force_offline)

        self.authenticate(force_offline=force_offline)
        self.connect_mqtt()

        if enable_ha:
            from ha_bridge import HomeAssistantBridge

            ha_config = self.config.get("homeassistant", {})
            if ha_config.get("enabled") or enable_ha:
                self.ha_bridge = HomeAssistantBridge(ha_config, self.devices)
                self.ha_bridge.command_callback = self._ha_command
                self.ha_bridge.connect()

        self._run_init_rules()

        log.info("Monitoring started (polling every %ds)", poll_interval)

        # Smart high-freq warm-up mode:
        # Enable high-frequency MQTT reporting for a short warm-up period to quickly
        # populate initial data (E3600/E3800 send telemetry in 3 alternating packet shapes).
        # After warm-up, disable high-freq to avoid burning cloud quota (error code 4026).
        # Only useful when cloud MQTT is connected — skip in offline/local-only mode.
        high_freq_warmup_seconds = self.config.get("high_freq_warmup_seconds", 60)
        if self.mqtt_client and high_freq_warmup_seconds > 0:
            log.info(
                "Enabling high-freq reporting for %ds warm-up period...", high_freq_warmup_seconds
            )
            self._enable_high_freq_reporting()
            warmup_start = time.time()

        time.sleep(3)
        cycle_elapsed = self._request_status_with_local_retries(poll_interval)

        try:
            while self._running:
                if poll_interval > 0:
                    cycle_remainder = cycle_elapsed % poll_interval
                    cycle_delay = (
                        poll_interval if cycle_remainder == 0 else poll_interval - cycle_remainder
                    )
                else:
                    cycle_delay = 0.0
                time.sleep(cycle_delay)
                # Check if warm-up period has ended — disable high-freq to save cloud quota
                if self.mqtt_client and high_freq_warmup_seconds > 0:
                    elapsed = time.time() - warmup_start
                    if elapsed >= high_freq_warmup_seconds:
                        log.info(
                            "Warm-up complete (%.0fs) — disabling high-freq to preserve cloud quota",
                            elapsed,
                        )
                        self._disable_high_freq_reporting()
                        high_freq_warmup_seconds = 0  # Prevent re-disabling on every loop

                if self._token_needs_refresh():
                    log.info("Refreshing token...")
                    try:
                        if self.mqtt_client:
                            self.mqtt_client.loop_stop()
                            self.mqtt_client.disconnect()
                    except Exception:
                        pass
                    self.authenticate(force_offline=force_offline)
                    self.connect_mqtt()
                    time.sleep(3)
                else:
                    # Issue #23: if we previously fell back to offline due to a
                    # transient cloud failure, periodically attempt to recover.
                    self._try_cloud_recovery()
                    self._recover_mqtt_connection()

                # Issue #23 (secondary): retry local HA MQTT if it failed to
                # connect at startup or was lost.
                if self.ha_bridge:
                    self.ha_bridge.try_reconnect()

                cycle_elapsed = self._request_status_with_local_retries(poll_interval)

        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            self._running = False
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            if self.ha_bridge:
                self.ha_bridge.disconnect()

    def stop(self):
        self._running = False
