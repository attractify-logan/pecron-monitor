"""Persisted power-to-energy integration for Home Assistant sensors."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

log = logging.getLogger("pecron")

ENERGY_CHANNELS = ("ac_input", "ac_output", "dc_output")
DEFAULT_MAX_GAP_SECONDS = 1800.0


def state_path(configured_path: Optional[str] = None) -> Path:
    """Resolve the energy-counter state path, with an environment override for tests."""
    override = os.environ.get("PECRON_ENERGY_STATE_PATH")
    if override:
        return Path(override).expanduser()
    if configured_path:
        return Path(configured_path).expanduser()
    return Path.home() / ".pecron-monitor-energy.json"


class EnergyIntegrator:
    """Integrate observed channel power into per-device persisted kWh counters."""

    def __init__(
        self,
        *,
        configured_path: Optional[str] = None,
        max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.path = state_path(configured_path)
        self.max_gap_seconds = float(max_gap_seconds)
        if not math.isfinite(self.max_gap_seconds) or self.max_gap_seconds <= 0:
            raise ValueError("energy_max_gap_seconds must be a positive finite number")
        self.clock = clock or time.monotonic
        self._totals = self._load()
        self._samples: dict[str, dict[str, tuple[float, float]]] = {}

    def update(self, device_key: str, readings: Mapping[str, object]) -> dict[str, float]:
        """Observe power readings and return counters for channels valid in this observation.

        Integration uses the trapezoidal rule between two valid observations. Missing channels
        leave their prior observation untouched because Pecron telemetry arrives in partial packet
        shapes. An explicitly unavailable value breaks that channel's continuity.
        """
        if not readings:
            return {}

        observed_at = float(self.clock())
        totals = self._totals.setdefault(device_key, {})
        samples = self._samples.setdefault(device_key, {})
        available: dict[str, float] = {}
        changed = False

        for channel in ENERGY_CHANNELS:
            if channel not in readings:
                continue

            power = self._valid_power(readings[channel])
            if power is None:
                samples.pop(channel, None)
                continue

            total = totals.setdefault(channel, 0.0)
            previous = samples.get(channel)
            samples[channel] = (observed_at, power)
            available[channel] = total
            if previous is None:
                continue

            previous_at, previous_power = previous
            elapsed = observed_at - previous_at
            if elapsed <= 0 or elapsed > self.max_gap_seconds:
                continue

            increment = ((previous_power + power) / 2.0) * elapsed / 3_600_000.0
            if increment > 0:
                totals[channel] = total + increment
                available[channel] = totals[channel]
                changed = True

        if changed:
            self._save()
        return available

    @staticmethod
    def _valid_power(raw: object) -> Optional[float]:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value

    def _load(self) -> dict[str, dict[str, float]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r") as state_file:
                payload = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            log.warning(
                "Could not read %s (%s); treating energy counters as empty.", self.path, error
            )
            return {}

        raw_devices = payload.get("devices", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_devices, dict):
            return {}

        devices: dict[str, dict[str, float]] = {}
        for device_key, raw_channels in raw_devices.items():
            if not isinstance(device_key, str) or not isinstance(raw_channels, dict):
                continue
            channels: dict[str, float] = {}
            for channel in ENERGY_CHANNELS:
                value = self._valid_energy(raw_channels.get(channel))
                if value is not None:
                    channels[channel] = value
            if channels:
                devices[device_key] = channels
        return devices

    @staticmethod
    def _valid_energy(raw: object) -> Optional[float]:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"devices": self._totals}
        fd, temporary_name = tempfile.mkstemp(prefix=".pecron-energy-", dir=str(self.path.parent))
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w") as state_file:
                json.dump(payload, state_file, indent=2, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            temporary_path.replace(self.path)
        except Exception:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise
