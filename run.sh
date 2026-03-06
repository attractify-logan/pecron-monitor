#!/usr/bin/env bash
# Wrapper script for systemd service.
# This indirection means systemd's cached ExecStart stays valid
# even after git branch checkouts or updates.
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
# Resolve symlinks to find the real script location
while [ -L "$SOURCE" ]; do
    DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    # If readlink returned a relative path, resolve it
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"

exec python3 "$SCRIPT_DIR/pecron_monitor.py" "$@"
