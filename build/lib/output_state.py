"""Snapshot persistence for the restore-outputs-after-shutdown feature (#59).

When a device transitions offline at low SoC or low voltage (likely a
low-battery shutdown), the monitor snapshots the user's current AC/DC switch
state to disk. When the device later transitions back online, the snapshot is
loaded and the worker re-applies the previous state. The on-disk format outlives
monitor restarts so a restart during the offline window doesn't lose the snapshot.

Storage: a single JSON file at `~/.pecron-monitor-state.json` (override via
`PECRON_STATE_PATH` env for tests). Schema:

    {
      "snapshots": {
        "<device_key>": {
          "ac_on": bool,
          "dc_on": bool,
          "soc_at_offline": int | null,
          "voltage_at_offline": float | null,
          "snapshotted_at": "<ISO8601 UTC>"
        },
        ...
      }
    }

Atomic writes via temp-file-and-rename. Missing/corrupt files load as empty.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("pecron")


def _state_path() -> Path:
    """Resolve the on-disk path. Honor PECRON_STATE_PATH for tests."""
    override = os.environ.get("PECRON_STATE_PATH")
    if override:
        return Path(override)
    return Path.home() / ".pecron-monitor-state.json"


@dataclass
class OutputSnapshot:
    ac_on: bool
    dc_on: bool
    soc_at_offline: Optional[int]
    snapshotted_at: str  # ISO8601 UTC
    voltage_at_offline: Optional[float] = None

    @classmethod
    def now(
        cls,
        ac_on: bool,
        dc_on: bool,
        soc_at_offline: Optional[int],
        voltage_at_offline: Optional[float] = None,
    ) -> "OutputSnapshot":
        return cls(
            ac_on=bool(ac_on),
            dc_on=bool(dc_on),
            soc_at_offline=int(soc_at_offline) if soc_at_offline is not None else None,
            voltage_at_offline=(
                float(voltage_at_offline) if voltage_at_offline is not None else None
            ),
            snapshotted_at=datetime.now(timezone.utc).isoformat(),
        )

    def age_seconds(self, *, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        try:
            taken_at = datetime.fromisoformat(self.snapshotted_at)
        except ValueError:
            return float("inf")
        if taken_at.tzinfo is None:
            taken_at = taken_at.replace(tzinfo=timezone.utc)
        return (now - taken_at).total_seconds()


def load_all() -> dict[str, OutputSnapshot]:
    """Load all snapshots from disk. Empty dict on missing/corrupt file."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        with path.open("r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s (%s); treating as empty.", path, e)
        return {}
    snapshots = data.get("snapshots", {}) if isinstance(data, dict) else {}
    out: dict[str, OutputSnapshot] = {}
    for dk, raw in snapshots.items():
        try:
            out[dk] = OutputSnapshot(**raw)
        except (TypeError, KeyError) as e:
            log.warning("Skipping malformed snapshot for %s: %s", dk, e)
    return out


def _save_all(snapshots: dict[str, OutputSnapshot]) -> None:
    """Atomic write: write to temp file in same dir, fsync, rename."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"snapshots": {dk: asdict(s) for dk, s in snapshots.items()}}
    fd, tmp_str = tempfile.mkstemp(prefix=".pecron-state-", dir=str(path.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def save(device_key: str, snap: OutputSnapshot) -> None:
    """Insert or overwrite the snapshot for one device."""
    snapshots = load_all()
    snapshots[device_key] = snap
    _save_all(snapshots)


def clear(device_key: str) -> bool:
    """Remove the snapshot for one device. Returns True if anything was removed."""
    snapshots = load_all()
    if device_key not in snapshots:
        return False
    del snapshots[device_key]
    _save_all(snapshots)
    return True


def get(device_key: str) -> Optional[OutputSnapshot]:
    return load_all().get(device_key)
