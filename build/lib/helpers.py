"""
Helper utilities for pecron-monitor.

Contains utility functions for data normalization, formatting, and
safe dictionary navigation used throughout the application.
"""


def _truthy(v):
    """Robust truthiness for device values (handles 0/1, '0'/'1', 'on'/'off', etc.)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "t", "yes", "y", "on", "open", "enabled"):
            return True
        if s in ("0", "false", "f", "no", "n", "off", "close", "closed", "disabled", ""):
            return False
        return True
    return bool(v)


def _fmt_dhm(minutes):
    """Format minutes as human-readable d/h/m string."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return None
    if m < 0 or m >= 65535:
        return None
    d = m // (60 * 24)
    h = (m % (60 * 24)) // 60
    mm = m % 60
    if d > 0:
        return f"{d}d{h:02d}h{mm:02d}m"
    return f"{h}h{mm:02d}m"


def _get_kv(kv: dict, paths, default=None):
    """Safely navigate nested kv dict. Accepts a single path tuple or a list of paths to try."""
    if not paths:
        return default
    # If it's a list of tuples, try each path
    if isinstance(paths, list):
        for path in paths:
            result = _get_kv_single(kv, path)
            if result is not None:
                return result
        return default
    # Single tuple path
    return _get_kv_single(kv, paths) if _get_kv_single(kv, paths) is not None else default


def _get_kv_single(kv: dict, path: tuple):
    """Safely navigate nested kv dict by a single path tuple."""
    obj = kv
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
        if obj is None:
            return None
    return obj
