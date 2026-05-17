"""Helpers for loading local-only hardware test configuration.

These scripts intentionally do not store live device IPs or auth keys in source
control. Provide device definitions via either:

- PECRON_HARDWARE_CONFIG=/path/to/hardware-devices.json
- PECRON_HARDWARE_JSON='[...]'
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


REQUIRED_FIELDS = {"device_key", "auth_key", "lan_ip"}


def _validate_devices(devices: object) -> list[dict]:
    if not isinstance(devices, list) or not devices:
        raise ValueError("Hardware config must be a non-empty JSON list of device objects")

    validated = []
    for index, device in enumerate(devices, start=1):
        if not isinstance(device, dict):
            raise ValueError(f"Device #{index} must be a JSON object")
        missing = REQUIRED_FIELDS - set(device.keys())
        if missing:
            missing_fields = ", ".join(sorted(missing))
            raise ValueError(f"Device #{index} is missing required field(s): {missing_fields}")

        device_key = device["device_key"]
        auth_key = device["auth_key"]
        lan_ip = device["lan_ip"]

        if not isinstance(device_key, str) or not device_key.strip():
            raise ValueError(f"Device #{index} has an invalid device_key")
        if not isinstance(auth_key, str) or not auth_key.strip():
            raise ValueError(f"Device #{index} has an invalid auth_key")
        if not isinstance(lan_ip, str) or not lan_ip.strip():
            raise ValueError(f"Device #{index} has an invalid lan_ip")

        validated.append(
            {
                "device_key": device_key.strip(),
                "auth_key": auth_key.strip(),
                "lan_ip": lan_ip.strip(),
            }
        )
    return validated


def load_hardware_devices() -> list[dict]:
    """Load hardware test device config from env-provided JSON or file path."""
    json_payload = os.getenv("PECRON_HARDWARE_JSON")
    config_path = os.getenv("PECRON_HARDWARE_CONFIG")

    if json_payload:
        return _validate_devices(json.loads(json_payload))

    if config_path:
        path = Path(config_path).expanduser()
        return _validate_devices(json.loads(path.read_text()))

    raise RuntimeError(
        "Hardware test configuration not provided. Set PECRON_HARDWARE_CONFIG to a local JSON file "
        "or PECRON_HARDWARE_JSON to a JSON array of device objects."
    )
