#!/usr/bin/env python3
"""
Pecron Battery Monitor & Controller — real-time monitoring and control
via local TCP (LAN) with cloud MQTT fallback. Works with any Pecron power station.

Usage:
    python pecron_monitor.py --setup        # Interactive setup wizard
    python pecron_monitor.py                # Start monitoring
    python pecron_monitor.py --local        # Run in offline/local-only mode (no cloud)
    python pecron_monitor.py --status       # One-shot status check
    python pecron_monitor.py --ac on        # Turn AC output on
    python pecron_monitor.py --ac off       # Turn AC output off
    python pecron_monitor.py --dc on        # Turn DC output on
    python pecron_monitor.py --dc off       # Turn DC output off
    python pecron_monitor.py --controls     # List all available controls for your model
    python pecron_monitor.py --control ac_switch_hm on   # Set any control by code
    python pecron_monitor.py --raw          # Dump raw JSON from device
    python pecron_monitor.py --homeassistant # Start with Home Assistant MQTT bridge
"""

__version__ = "0.6.1"

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
    parser.add_argument("--auto", action="store_true", help="Auto mode for setup (reads PECRON_EMAIL, PECRON_PASSWORD, PECRON_REGION from env)")
    parser.add_argument("--local", action="store_true",
                        help="Run in offline/local-only mode (no cloud, uses cached config)")
    parser.add_argument("--no-ble", action="store_true",
                        help="Disable Bluetooth (BLE) transport — use WiFi TCP or cloud only")
    parser.add_argument("--status", action="store_true", help="One-shot status check")
    parser.add_argument("--ac", choices=["on", "off"], help="Turn AC output on/off")
    parser.add_argument("--dc", choices=["on", "off"], help="Turn DC output on/off")
    parser.add_argument("--homeassistant", action="store_true", help="Enable HA MQTT bridge")
    parser.add_argument("--raw", action="store_true", help="Dump raw JSON data from device")
    parser.add_argument("--controls", action="store_true", help="List available controls from TSL")
    parser.add_argument("--control", nargs=2, metavar=("CODE", "VALUE"),
                        help="Set any control: --control ac_switch_hm true")
    parser.add_argument("--diagnose", action="store_true",
                        help="Run diagnostics: verify device binding, show MQTT topics, wait for data")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="Config file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    if args.setup:
        setup_wizard(auto=args.auto)
        return

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found at {config_path}")
        print("Run 'python pecron_monitor.py --setup' to create it.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    monitor = PecronMonitor(config, no_ble=args.no_ble)

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
                print(f"  id={info['id']:3d}  {rw}  {info.get('type','?'):6s}  {code}  — {info.get('desc', '')}")
                if info.get("type") == "ENUM" and code in ENUM_LABELS:
                    labels = ENUM_LABELS[code]
                    opts = "  ".join(f"{k}={v}" for k, v in sorted(labels.items()))
                    print(f"                       {opts}")
        return

    if args.raw:
        monitor = PecronMonitor(config, no_ble=args.no_ble)
        monitor.authenticate()
        monitor.connect_mqtt()
        time.sleep(3)
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
                    path_str = str(field_paths) if isinstance(field_paths, (list, tuple)) else field_paths
                    print(f"{field_name:<30} {str(value):<15} {path_str}")
            
            # Print full raw JSON
            print(f"\n{'=' * 60}")
            print("RAW JSON DATA:")
            print(f"{'=' * 60}")
            print(json.dumps({dk: kv}, indent=2, default=str))
        if not monitor.latest_data:
            print("No data received — device may be offline.")
        if monitor.mqtt_client:
            monitor.mqtt_client.loop_stop()
            monitor.mqtt_client.disconnect()
        return

    if args.control:
        code, val = args.control
        # Parse value
        if val.lower() in ("true", "on", "1"):
            parsed_val = True
        elif val.lower() in ("false", "off", "0"):
            parsed_val = False
        else:
            try:
                parsed_val = int(val)
            except ValueError:
                print(f"Invalid value: {val}")
                sys.exit(1)
        monitor = PecronMonitor(config, no_ble=args.no_ble)
        monitor.authenticate()
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

    if args.diagnose:
        from cloud_api import verify_device, get_device_online_status
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
                    print(f"      This is cosmetic — the API controls the name shown.")

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
                        print(f"   → Update your config.yaml: product_key: \"{cat_pk}\"")
                        found = True
                        break
                if not found:
                    print(f"   ❌ Device key {dk} not found under ANY product.")
                    print(f"   ⚠️  Double-check your device key from the Pecron app (Device Info → Device Key or Device Code).")
                    print(f"      Should be 12 hex characters (MAC address) like AABBCCDDEEFF")

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
        monitor = PecronMonitor(config, no_ble=args.no_ble)
        monitor.authenticate()
        monitor.connect_mqtt()
        time.sleep(3)
        monitor._request_status()

        for i in range(12):
            time.sleep(1)
            if monitor.latest_data:
                break
            if i % 3 == 2:
                print(f"   Waiting... ({i+1}s)")

        if monitor.latest_data:
            print("   ✅ Data received!")
            for dk, kv in monitor.latest_data.items():
                print(f"\n   Device {dk}: {len(kv)} data fields")
                battery = _get_kv(kv, SENSOR_FIELDS["battery_percent"])
                if battery is not None:
                    print(f"   Battery: {battery}%")
                else:
                    print(f"   ⚠️  Battery field not found in response")
                    print(f"   Raw top-level keys: {list(kv.keys())}")
        else:
            print("   ❌ No data received after 15 seconds")
            print("\n   Possible causes:")
            print("   • Device is offline (check Pecron app)")
            print("   • Wrong device_key (used 'Device Code' instead of 'Device Key'?)")
            print("   • Wrong product_key (try --setup to auto-detect)")
            print("   • Device WiFi module is sleeping (open Pecron app to wake it)")
            print("\n   Run with -v for detailed MQTT debug logs:")
            print(f"   python3 pecron_monitor.py --diagnose -v")

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
        monitor.status_once(force_offline=args.local)
    else:
        monitor.run(
            enable_ha=args.homeassistant or config.get("homeassistant", {}).get("enabled", False),
            force_offline=args.local,
        )


if __name__ == "__main__":
    main()
