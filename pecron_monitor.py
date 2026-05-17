#!/usr/bin/env python3
"""
Pecron Battery Monitor & Controller — real-time monitoring and control
via local TCP (LAN) with cloud MQTT fallback. Works with any Pecron power station.

Usage:
    pecron-monitor --setup        # Interactive setup wizard
    pecron-monitor                # Start monitoring
    pecron-monitor --local        # Run in offline/local-only mode (no cloud)
    pecron-monitor --status       # One-shot status check
    pecron-monitor --ac on        # Turn AC output on
    pecron-monitor --ac off       # Turn AC output off
    pecron-monitor --dc on        # Turn DC output on
    pecron-monitor --dc off       # Turn DC output off
    pecron-monitor --controls     # List all available controls for your model
    pecron-monitor --control ac_switch_hm on   # Set any control by code
    pecron-monitor --probe-control ac_discharge_power_hm --probe-max 20
                                            # Probe valid values starting at 0
    pecron-monitor --raw          # Dump raw JSON from device
    pecron-monitor --homeassistant # Start with Home Assistant MQTT bridge
"""

__version__ = "0.7.11"

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import yaml

from constants import REGIONS, DEFAULT_CONTROLS, SENSOR_FIELDS, ENUM_LABELS
from cloud_api import login, get_product_catalog, get_product_tsl
from helpers import _get_kv
from monitor import PecronMonitor
from setup_wizard import setup_wizard

CONFIG_PATH = Path(__file__).parent / "config.yaml"
log = logging.getLogger("pecron")


def main():
    parser = argparse.ArgumentParser(description="Pecron Battery Monitor & Controller")
    parser.add_argument("--version", action="version", version=f"pecron-monitor {__version__}")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto mode for setup (reads PECRON_EMAIL, PECRON_PASSWORD, PECRON_REGION from env)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run in offline/local-only mode (no cloud, uses cached config)",
    )
    parser.add_argument(
        "--nolocal",
        action="store_true",
        help="Skip setting up local TCP/BLE transports in authenticate()",
    )
    parser.add_argument(
        "--no-ble",
        action="store_true",
        help="Disable Bluetooth (BLE) transport — use WiFi TCP or cloud only",
    )
    parser.add_argument(
        "--rest-only",
        action="store_true",
        help="Use REST API only (disable MQTT and local transports)",
    )
    parser.add_argument("--status", action="store_true", help="One-shot status check")
    parser.add_argument("--ac", choices=["on", "off"], help="Turn AC output on/off")
    parser.add_argument("--dc", choices=["on", "off"], help="Turn DC output on/off")
    parser.add_argument("--homeassistant", action="store_true", help="Enable HA MQTT bridge")
    parser.add_argument("--raw", action="store_true", help="Dump raw JSON data from device")
    parser.add_argument("--controls", action="store_true", help="List available controls from TSL")
    parser.add_argument(
        "--control",
        nargs=2,
        metavar=("CODE", "VALUE"),
        help="Set any control: --control ac_switch_hm true",
    )
    parser.add_argument(
        "--probe-control",
        metavar="CODE",
        help="Probe contiguous supported values for a control, starting at 0",
    )
    parser.add_argument(
        "--probe-min",
        type=int,
        default=0,
        help="Minimum probe value when using --probe-control (default: 0)",
    )
    parser.add_argument(
        "--probe-max",
        type=int,
        default=255,
        help="Maximum probe value when using --probe-control (default: 255)",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Run diagnostics: verify device binding, show MQTT topics, wait for data",
    )
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="Config file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.setup:
        setup_wizard(auto=args.auto)
        return

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found at {config_path}")
        print("Run 'pecron-monitor --setup' to create it.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    monitor = PecronMonitor(config, no_ble=args.no_ble, rest_only=args.rest_only)

    def _signal_handler(sig, frame):
        monitor.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.controls:
        # List available controls for all configured devices
        token_data = login(config["email"], config["password"], REGIONS[config["region"]])
        catalog = get_product_catalog(token_data["token"], REGIONS[config["region"]])
        for d in config.get("devices", []):
            pk, dk = d["product_key"], d["device_key"]
            name = catalog.get(pk, d.get("name", "Unknown"))
            tsl = get_product_tsl(token_data["token"], REGIONS[config["region"]], pk)
            print(f"\n{name} ({dk}):")
            if not tsl:
                print("  (TSL not available — using defaults)")
                tsl = DEFAULT_CONTROLS
            for code, info in sorted(tsl.items(), key=lambda x: x[1]["id"]):
                rw = "RW" if "W" in info.get("access", "R").upper() else "RO"
                print(
                    f"  id={info['id']:3d}  {rw}  {info.get('type', '?'):6s}  {code}  — {info.get('desc', '')}"
                )
                if info.get("type") == "ENUM" and code in ENUM_LABELS:
                    labels = ENUM_LABELS[code]
                    opts = "  ".join(f"{k}={v}" for k, v in sorted(labels.items()))
                    print(f"                       {opts}")
        return

    if args.raw:
        monitor = PecronMonitor(config, no_ble=args.no_ble, rest_only=args.rest_only)
        monitor.authenticate(force_offline=args.local, skip_local=args.nolocal)
        monitor.connect_mqtt()
        time.sleep(3)
        # Enable high-freq reporting for devices that need it (E3600/E3800)
        if monitor.mqtt_client:
            monitor._enable_high_freq_reporting()
            time.sleep(2)  # Give device time to switch modes
        monitor._request_status()
        time.sleep(5)
        for dk, kv in monitor.latest_data.items():
            # Print human-readable field mappings
            print(f"\n{'=' * 60}")
            print(f"Device: {dk}")
            print(f"{'=' * 60}")
            print("\nKNOWN FIELDS (from SENSOR_FIELDS mapping):")
            print(f"{'Field Name':<30} {'Value':<15} Raw Key(s)")
            print("-" * 60)
            for field_name, field_paths in SENSOR_FIELDS.items():
                value = _get_kv(kv, field_paths, None)
                if value is not None:
                    # Show the actual path(s) used
                    path_str = (
                        str(field_paths) if isinstance(field_paths, (list, tuple)) else field_paths
                    )
                    print(f"{field_name:<30} {str(value):<15} {path_str}")

            # Print unknown fields not referenced by SENSOR_FIELDS
            def _collect_keys(p):
                if isinstance(p, (list, tuple)):
                    keys = []
                    for sub in p:
                        keys.extend(_collect_keys(sub))
                    return keys
                if isinstance(p, str):
                    return [p]
                return []

            referenced = set()
            for field_paths in SENSOR_FIELDS.values():
                for k in _collect_keys(field_paths):
                    referenced.add(k)

            unknown_keys = [k for k in kv.keys() if k not in referenced]
            if unknown_keys:
                print(f"\n{'=' * 60}")
                print("UNKNOWN FIELDS (not in SENSOR_FIELDS):")
                print(f"{'=' * 60}")
                for k in sorted(unknown_keys):
                    try:
                        val_str = json.dumps(kv[k], default=str)
                    except Exception:
                        val_str = str(kv[k])
                    print(f"{k}: {val_str}")

            # Print full raw JSON
            print(f"\n{'=' * 60}")
            print("RAW JSON DATA:")
            print(f"{'=' * 60}")
            print(json.dumps({dk: kv}, indent=2, default=str))
        if not monitor.latest_data:
            print("No data received — device may be offline.")
        # Don't leave the device stranded in high-freq mode after --raw exits.
        if monitor.mqtt_client:
            monitor._disable_high_freq_reporting()
            monitor.mqtt_client.loop_stop()
            monitor.mqtt_client.disconnect()
        return

    if args.control:
        code, val = args.control
        # Parse value
        if val.lower() in ("true", "on"):
            parsed_val = True
        elif val.lower() in ("false", "off"):
            parsed_val = False
        else:
            try:
                parsed_val = int(val)
            except ValueError:
                print(f"Invalid value: {val}")
                sys.exit(1)
        monitor = PecronMonitor(config, no_ble=args.no_ble, rest_only=args.rest_only)
        monitor.authenticate(force_offline=args.local, skip_local=args.nolocal)
        monitor.connect_mqtt()
        time.sleep(3)
        for device in monitor.devices:
            monitor.send_control(device["device_key"], code, parsed_val)
        time.sleep(3)
        monitor._request_status()
        time.sleep(5)
        for dk, kv in monitor.latest_data.items():
            print(f"Device {dk}: sent {code}={parsed_val}")
        if monitor.mqtt_client:
            monitor.mqtt_client.loop_stop()
            monitor.mqtt_client.disconnect()
        return

    if args.probe_control:
        if args.probe_max < args.probe_min:
            print("--probe-max must be >= --probe-min")
            sys.exit(1)

        def _format_value_set(values):
            if not values:
                return "(none)"
            if values == list(range(values[0], values[-1] + 1)):
                if len(values) == 1:
                    return str(values[0])
                return f"{values[0]}-{values[-1]}"
            return ", ".join(str(v) for v in values)

        monitor = PecronMonitor(config, no_ble=args.no_ble, rest_only=args.rest_only)
        monitor.authenticate(force_offline=args.local, skip_local=args.nolocal)
        if not monitor.offline_mode:
            monitor.connect_mqtt()
            time.sleep(3)

        print(
            f"Probing control '{args.probe_control}' from {args.probe_min} upward (max={args.probe_max})"
        )
        print(
            f"Mode: {'local-only' if args.local else ('cloud/remote (local skipped)' if args.nolocal else 'auto local+cloud')}"
        )

        for device in monitor.devices:
            dk = device["device_key"]
            result = monitor.probe_control_values(
                dk,
                args.probe_control,
                min_value=args.probe_min,
                max_value=args.probe_max,
            )
            valid = result["valid_values"]
            print(f"\nDevice {dk}")
            print(f"  Control: {args.probe_control}")
            print(f"  Valid values: {_format_value_set(valid)}")
            if result["reason"] == "max_reached":
                print(f"  Stopped at: max probe value {args.probe_max}")
            else:
                print(
                    f"  Stopped at: {result['stop_value']} (reason={result['reason']}, readback={result['last_readback']})"
                )

        if monitor.mqtt_client:
            monitor.mqtt_client.loop_stop()
            monitor.mqtt_client.disconnect()
        return

    if args.diagnose:
        from cloud_api import verify_device

        print("\n🔍 Pecron Monitor Diagnostics\n")
        region = REGIONS[config["region"]]

        # Step 1: Auth
        print("1. Authentication...")
        try:
            token_data = login(config["email"], config["password"], region)
            print(f"   ✅ Logged in (uid: {token_data['uid']})")
        except Exception as e:
            print(f"   ❌ Login failed: {e}")
            sys.exit(1)

        # Step 2: Product catalog
        print("\n2. Product catalog...")
        catalog = get_product_catalog(token_data["token"], region)
        print(f"   Found {len(catalog)} products in catalog")

        # Step 3: Device verification
        print("\n3. Device verification...")
        for d in config.get("devices", []):
            pk, dk = d["product_key"], d["device_key"]
            config_name = d.get("name", "Unknown")
            catalog_name = catalog.get(pk, "NOT IN CATALOG")
            print(f"\n   Device: {config_name}")
            print(f"   Config product_key: {pk}")
            print(f"   Config device_key:  {dk}")
            print(f"   Catalog name for pk: {catalog_name}")

            info = verify_device(token_data["token"], region, pk, dk)
            if info:
                api_name = info.get("productName", "?")
                print(f"   ✅ Device verified — API says: {api_name}")
                if api_name != config_name:
                    print(f"   ⚠️  Name mismatch: config='{config_name}' vs API='{api_name}'")
                    print("      This is cosmetic — the API controls the name shown.")

                # Show binding info
                for key in ["deviceKey", "productKey", "deviceName", "mac", "online"]:
                    if key in info:
                        print(f"   {key}: {info[key]}")
            else:
                print(f"   ❌ Device NOT found with pk={pk} dk={dk}")
                print(f"\n   Searching all products for dk={dk}...")
                found = False
                for cat_pk, cat_name in catalog.items():
                    alt_info = verify_device(token_data["token"], region, cat_pk, dk)
                    if alt_info:
                        print(f"   ✅ Found under: {cat_name} (pk={cat_pk})")
                        print(f'   → Update your config.yaml: product_key: "{cat_pk}"')
                        found = True
                        break
                if not found:
                    print(f"   ❌ Device key {dk} not found under ANY product.")
                    print(
                        "   ⚠️  Double-check your device key from the Pecron app (Device Info → Device Key or Device Code)."
                    )
                    print("      Should be 12 hex characters (MAC address) like AABBCCDDEEFF")

            # TSL
            tsl = get_product_tsl(token_data["token"], region, pk)
            if tsl:
                rw_count = sum(1 for v in tsl.values() if "W" in v.get("access", "R").upper())
                print(f"   TSL: {len(tsl)} properties ({rw_count} writable)")
            else:
                print(f"   ⚠️  TSL not available for pk={pk}")

        # Step 4: MQTT test
        print("\n4. MQTT connectivity test...")
        print("   Connecting and waiting 15 seconds for data...\n")
        monitor = PecronMonitor(config, no_ble=args.no_ble, rest_only=args.rest_only)
        monitor.authenticate(skip_local=args.nolocal)
        monitor.connect_mqtt()
        time.sleep(3)
        monitor._request_status()

        for i in range(12):
            time.sleep(1)
            if monitor.latest_data:
                break
            if i % 3 == 2:
                print(f"   Waiting... ({i + 1}s)")

        if monitor.latest_data:
            print("   ✅ Data received!")
            for dk, kv in monitor.latest_data.items():
                print(f"\n   Device {dk}: {len(kv)} data fields")
                battery = _get_kv(kv, SENSOR_FIELDS["battery_percent"])
                if battery is not None:
                    print(f"   Battery: {battery}%")
                else:
                    print("   ⚠️  Battery field not found in response")
                    print(f"   Raw top-level keys: {list(kv.keys())}")
        else:
            print("   ❌ No data received after 15 seconds")
            print("\n   Possible causes:")
            print("   • Device is offline (check Pecron app)")
            print("   • Wrong device_key (used 'Device Code' instead of 'Device Key'?)")
            print("   • Wrong product_key (try --setup to auto-detect)")
            print("   • Device WiFi module is sleeping (open Pecron app to wake it)")
            print("\n   Run with -v for detailed MQTT debug logs:")
            print("   pecron-monitor --diagnose -v")

        if monitor.mqtt_client:
            monitor.mqtt_client.loop_stop()
            monitor.mqtt_client.disconnect()
        print("\n✅ Diagnostics complete")
        return

    if args.ac is not None or args.dc is not None:
        monitor.one_shot_command(
            ac=(args.ac == "on") if args.ac else None,
            dc=(args.dc == "on") if args.dc else None,
            force_offline=args.local,
        )
    elif args.status:
        monitor.status_once(force_offline=args.local, skip_local=args.nolocal)
    else:
        monitor.run(
            enable_ha=args.homeassistant or config.get("homeassistant", {}).get("enabled", False),
            force_offline=args.local,
        )


if __name__ == "__main__":
    main()
