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

from helpers import _truthy
from energy_state import DEFAULT_MAX_GAP_SECONDS, EnergyIntegrator
from ha_discovery import BRIDGE_AVAILABILITY_TOPIC, HomeAssistantDiscoveryMixin
from ha_state import HomeAssistantStateMixin

log = logging.getLogger("pecron")


class HomeAssistantBridge(HomeAssistantDiscoveryMixin, HomeAssistantStateMixin):
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
        self._discovery_cleared = False

        # Availability: a device goes "offline" in HA after stale_after seconds
        # with no reading from any source, so entities show unavailable rather
        # than frozen values. Default: five polls at the 120 s recommended interval.
        self.stale_after = float(ha_config.get("stale_after", 600))
        self._started_at = None
        self._last_seen: dict = {}  # device_key -> time of last publish_state
        self._device_online: dict = {}  # device_key -> last published availability
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
        # (device_key, slot) set: expansion-pack slots seen with a pack attached.
        self._deferred_packs_published: set = set()

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
        self._mark_started()
        self._connect_attempt()

    def _mark_started(self):
        if self._started_at is None:
            self._started_at = time.time()

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
        client.will_set(BRIDGE_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)

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
                client.publish(BRIDGE_AVAILABILITY_TOPIC, "online", qos=1, retain=True)
                for dk, online in self._device_online.items():
                    client.publish(
                        f"pecron/{dk}/availability",
                        "online" if online else "offline",
                        qos=1,
                        retain=True,
                    )
                # Read back our retained state once so the first payload after a
                # restart carries every field, not just the ones in the first packet.
                for device in self.devices:
                    client.subscribe(f"pecron/{device['device_key']}/state", qos=1)

        def on_disconnect(client, ud, disconnect_flags, rc, props=None):
            # paho auto-reconnect handles this after a successful initial connect,
            # but we flip the flag so try_reconnect() is a no-op until paho gives up.
            if self._connected:
                log.warning("Home Assistant MQTT bridge disconnected (rc=%s)", rc)
            self._connected = False

        def on_message(client, ud, msg):
            # Handle HA commands. An exception here would stop paho's network
            # thread, so never let one escape.
            try:
                parts = msg.topic.split("/")
                if len(parts) == 3 and parts[2] == "state":
                    self._on_state_message(msg)
                    return
                if len(parts) == 4 and parts[3] == "set":
                    if getattr(msg, "retain", False):
                        # Home Assistant never retains commands. A retained one
                        # would be replayed on every reconnect.
                        log.warning(
                            "Ignoring retained command on %s; clear it with an empty "
                            "retained publish",
                            msg.topic,
                        )
                        return
                    dk = parts[1]
                    ctrl = parts[2]
                    payload = msg.payload.decode("utf-8", errors="replace").strip().upper()
                    self._handle_command(dk, ctrl, payload)
            except Exception:
                log.exception("Home Assistant command on %s failed", msg.topic)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=self._retry_interval)
        try:
            # Assign before connecting: on_connect publishes discovery through
            # self.client and can run before loop_start() returns.
            self.client = client
            client.connect(host, port)
            client.loop_start()
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

    def _on_state_message(self, msg):
        """Seed the state cache from the retained state of the previous run.
        Only the retained copy counts; live echoes of our own publishes are ignored."""
        if not getattr(msg, "retain", False):
            return
        device_key = msg.topic.split("/")[1]
        if self.client is not None:
            self.client.unsubscribe(msg.topic)
        try:
            retained = json.loads(msg.payload)
        except (TypeError, ValueError):
            return
        if not isinstance(retained, dict):
            return
        cache = self._state_cache.setdefault(device_key, {})
        for key, value in retained.items():
            cache.setdefault(key, value)
        log.debug("Seeded HA state for %s with %d retained fields", device_key, len(retained))

    def _set_device_online(self, device_key: str, online: bool):
        if self._device_online.get(device_key) == online:
            return
        self._device_online[device_key] = online
        if self._connected and self.client is not None:
            self.client.publish(
                f"pecron/{device_key}/availability",
                "online" if online else "offline",
                qos=1,
                retain=True,
            )
        if not online:
            log.warning(
                "No reading from %s for %.0fs; marking it unavailable in Home Assistant",
                device_key,
                self.stale_after,
            )

    def check_availability(self):
        """Mark devices unavailable after stale_after seconds without a reading.
        Called from the monitor's main loop."""
        now = time.time()
        for device in self.devices:
            dk = device["device_key"]
            last = self._last_seen.get(dk, self._started_at)
            if last is not None and now - last > self.stale_after:
                self._set_device_online(dk, False)

    def disconnect(self):
        if self.client:
            try:
                info = self.client.publish(BRIDGE_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=2)
            except Exception:
                pass
            self.client.loop_stop()
            self.client.disconnect()
