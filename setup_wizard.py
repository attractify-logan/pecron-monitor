"""
Interactive setup wizard for pecron-monitor.

Guides users through account setup, device discovery, and configuration.
"""

import getpass
import os
import shutil
import subprocess
import yaml
from pathlib import Path

from cloud_api import login, get_user_devices, get_product_catalog, verify_device, get_product_tsl

try:
    from local_transport import get_auth_key
except ImportError:
    get_auth_key = None
from constants import REGIONS
from lan_scan import _setup_lan_discovery, discover_devices

HAS_LOCAL = get_auth_key is not None

try:
    from local_transport import scan_ble_devices, HAS_BLE
except ImportError:
    HAS_BLE = False

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _warn_if_config_is_readable(path: Path) -> None:
    """Warn when an existing config may expose credentials to other users."""
    if os.name != "posix" or not path.exists():
        return
    if path.stat().st_mode & 0o077:
        print(f"⚠️  {path} is readable by group/other users; run chmod 600 {path}")


def _secure_config_permissions(path: Path) -> None:
    """Restrict config.yaml to the current user on POSIX systems."""
    if os.name == "posix":
        os.chmod(path, 0o600)


def setup_wizard(auto=False):
    print("\n🔋 Pecron Monitor Setup\n")
    _warn_if_config_is_readable(CONFIG_PATH)

    if auto:
        # Read from environment variables
        email = os.getenv("PECRON_EMAIL")
        password = os.getenv("PECRON_PASSWORD")
        region = os.getenv("PECRON_REGION", "na").strip().lower()

        if not email or not password:
            print("❌ Auto mode requires PECRON_EMAIL and PECRON_PASSWORD environment variables")
            return

        if region not in REGIONS:
            print(f"Invalid region '{region}', using 'na'")
            region = "na"

        print(f"Auto mode: email={email}, region={region}")
    else:
        email = input("Pecron account email: ").strip()
        password = getpass.getpass("Pecron account password: ").strip()

        print("\nRegions:")
        print("  na — North America")
        print("  eu — Europe")
        print("  cn — China")
        region = input("Region [na]: ").strip().lower() or "na"
        if region not in REGIONS:
            print(f"Invalid region '{region}', using 'na'")
            region = "na"

    print("\nTesting login...")
    try:
        token_data = login(email, password, REGIONS[region])
        print(f"✅ Login successful (uid: {token_data['uid']})")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return

    # Try to discover devices from account first (most reliable — gives correct pk/dk)
    print("\n--- Device Setup ---")
    account_devices = get_user_devices(token_data["token"], REGIONS[region])
    devices = []
    use_manual = False

    if account_devices:
        if auto:
            # Auto mode: select all devices
            print(f"Auto mode: selecting all {len(account_devices)} device(s)")
            for d in account_devices:
                devices.append(d)
                print(f"  ✅ Added: {d['name']} ({d['device_key']})")
        else:
            print(f"Found {len(account_devices)} device(s) on your account:\n")
            for i, d in enumerate(account_devices, 1):
                print(f"  {i}. {d['name']}  (dk={d['device_key']})")
            print(f"  {len(account_devices) + 1}. Skip — enter device key manually instead")
            print("")
            default_sel = ",".join(str(i + 1) for i in range(len(account_devices)))
            choice = (
                input(f"Select devices to monitor (e.g. 1 or 1,2) [{default_sel}]: ").strip()
                or default_sel
            )

            for c in choice.split(","):
                c = c.strip()
                try:
                    idx = int(c) - 1
                    if idx == len(account_devices):
                        use_manual = True
                    elif 0 <= idx < len(account_devices):
                        d = account_devices[idx]
                        devices.append(d)
                        print(f"  ✅ Added: {d['name']} ({d['device_key']})")
                except ValueError:
                    pass
    else:
        print("Could not auto-discover devices from your account.")
        use_manual = True

    if use_manual or not devices:
        catalog = get_product_catalog(token_data["token"], REGIONS[region])
        print("\nManual device entry:")
        print("You need your device key (MAC address). Find it in the Pecron app:")
        print("  Device → Settings (⚙️) → Device Info → Device Key")
        print("  It looks like: AABBCCDDEEFF (12 hex characters)")
        print("")
        print("  (Some app versions label this 'Device Code' instead of 'Device Key' — same thing)")
        print("")

    while use_manual:
        dk = input("Device Key (or press Enter to finish): ").strip().upper()
        if not dk:
            break

        print("\nHow would you like to identify your product?")
        print("  1. Auto-detect (scan all models) [default]")
        print("  2. Select from list")
        method = input("Choose [1]: ").strip() or "1"

        if method == "2":
            # Manual selection from catalog
            sorted_products = sorted(catalog.items(), key=lambda x: x[1])
            print("\nAvailable models:")
            for i, (pk, name) in enumerate(sorted_products, 1):
                print(f"  {i:2d}. {name}  (pk={pk})")
            choice = input(f"Select [1-{len(sorted_products)}]: ").strip()
            try:
                idx = int(choice) - 1
                pk, name = sorted_products[idx]
                print(f"  Verifying {name} with key {dk}...", end="", flush=True)
                info = verify_device(token_data["token"], REGIONS[region], pk, dk)
                if info:
                    api_name = info.get("productName", name)
                    print(f"\r  ✅ Verified: {api_name} ({dk})")
                    devices.append({"product_key": pk, "device_key": dk, "name": api_name})
                else:
                    print(f"\r  ❌ Device {dk} not found under {name}.")
                    print("  Try auto-detect, or double-check your device key.")
            except (ValueError, IndexError):
                print("  Invalid selection.")
        else:
            # Auto-detect — try all matching product keys
            matches = []
            print("  Scanning all models...", end="", flush=True)
            for pk, name in catalog.items():
                info = verify_device(token_data["token"], REGIONS[region], pk, dk)
                if info:
                    api_name = info.get("productName", name)
                    matches.append({"pk": pk, "name": api_name, "info": info})

            if len(matches) == 1:
                m = matches[0]
                print(f"\r  ✅ Found: {m['name']} ({dk})")
                devices.append({"product_key": m["pk"], "device_key": dk, "name": m["name"]})
            elif len(matches) > 1:
                print(f"\r  Found {len(matches)} matching product entries:")
                for i, m in enumerate(matches, 1):
                    print(f"    {i}. {m['name']}  (pk={m['pk']})")
                print("  ℹ️  Multiple product keys match your device. If monitoring shows")
                print("     'device is not bound', try --setup again and select a different one.")
                choice = input(f"  Select [1-{len(matches)}] (default=1): ").strip() or "1"
                try:
                    idx = int(choice) - 1
                    m = matches[idx]
                except (ValueError, IndexError):
                    m = matches[0]
                print(f"  ✅ Using: {m['name']} (pk={m['pk']})")
                devices.append({"product_key": m["pk"], "device_key": dk, "name": m["name"]})
            else:
                print(f"\r  ❌ Device {dk} not found. Check the key and try again.")

    if not devices:
        print("⚠️  No devices added. You can add them manually to config.yaml later.")

    # Fetch TSL and auth keys for each device
    print("\n--- Fetching Device Metadata ---")
    for d in devices:
        pk = d["product_key"]
        dk = d["device_key"]
        print(f"  Fetching TSL (controls metadata) for {d.get('name', dk)}...", end="", flush=True)
        try:
            tsl = get_product_tsl(token_data["token"], REGIONS[region], pk)
            if tsl:
                d["tsl_cache"] = tsl
                print(f" ✅ ({len(tsl)} properties)")
            else:
                print(" ⚠️  Using defaults")
        except Exception as e:
            print(f" ❌ ({e})")

        # Fetch auth key for all devices
        if get_auth_key and not d.get("auth_key"):
            print(f"  Fetching encryption key for {d.get('name', dk)}...", end="", flush=True)
            try:
                auth_key = get_auth_key(token_data["token"], REGIONS[region], pk, dk)
                d["auth_key"] = auth_key
                print(" ✅")
            except Exception as e:
                print(f" ⚠️  ({e})")

    # --- Local / BLE Discovery (optional) ---
    if (HAS_LOCAL or HAS_BLE) and devices:
        if auto:
            # Auto mode: run LAN discovery automatically
            if HAS_LOCAL:
                print("\n--- Auto-Discovery (LAN) ---")
                print("Scanning local network for Pecron devices...")
                discovered = discover_devices(devices, timeout=0.5)

                if discovered:
                    for dk, ip in discovered.items():
                        for d in devices:
                            if d["device_key"] == dk:
                                d["lan_ip"] = ip
                                print(f"  ✅ {d.get('name', dk)} → {ip}")
                                break

                # Show summary table
                print("\n--- Auto-Discovery Summary ---")
                print(f"{'Device':<30} {'IP Address':<16} {'Auth Key':<10}")
                print("-" * 60)
                for d in devices:
                    name = d.get("name", d["device_key"])[:28]
                    ip = d.get("lan_ip", "Not found")
                    auth = "✅" if d.get("auth_key") else "❌"
                    print(f"{name:<30} {ip:<16} {auth:<10}")
        else:
            print("\n--- Local Monitoring (optional) ---")
            print("Monitor your Pecron without internet using WiFi TCP or Bluetooth.\n")
            print("  WiFi TCP — device and computer on same network (faster)")
            print("  Bluetooth — no network needed, ~30ft range (great for vanlife)\n")

            if HAS_LOCAL:
                do_lan = input("Scan for devices on WiFi LAN? [Y/n]: ").strip().lower() != "n"
                if do_lan:
                    devices = _setup_lan_discovery(devices, token_data["token"], REGIONS[region])

                # Manual IP entry for devices that weren't found or skipped
                print("\n--- Manual LAN IP Entry (optional) ---")
                for d in devices:
                    if d.get("lan_ip"):
                        continue  # Already configured
                    manual = input(
                        f"  Enter LAN IP for {d.get('name', d['device_key'])} (or press Enter to skip): "
                    ).strip()
                    if manual:
                        d["lan_ip"] = manual
                        # Fetch auth key if not already cached
                        if not d.get("auth_key"):
                            try:
                                print("  Fetching encryption key...", end="", flush=True)
                                auth_key = get_auth_key(
                                    token_data["token"],
                                    REGIONS[region],
                                    d["product_key"],
                                    d["device_key"],
                                )
                                d["auth_key"] = auth_key
                                print(" ✅")
                            except Exception as e:
                                print(f" ❌ ({e})")

            if HAS_BLE:
                do_ble = input("Scan for devices via Bluetooth? [Y/n]: ").strip().lower() != "n"
                if do_ble:
                    print("  Scanning for Pecron BLE devices...")
                    found = scan_ble_devices(timeout=10.0)
                    if found:
                        for addr, name in found:
                            print(f"  Found: {name} @ {addr}")
                            # Match to configured device by suffix
                            suffix = name.split("_")[-1] if "_" in name else ""
                            for d in devices:
                                if d["device_key"].upper().endswith(suffix):
                                    d["ble_address"] = addr
                                    d["ble"] = True
                                    print(f"    → Matched to {d.get('name', d['device_key'])}")
                    else:
                        print("  No Pecron BLE devices found nearby.")
                        print("  Make sure your Pecron is powered on and Bluetooth is enabled.")

    if auto:
        # Auto mode: use defaults
        poll = "70"
        threshold = "20"
        tg_enabled = False
        tg_token = tg_chat = ""
        ha_enabled = False
        ha_host = ha_user = ha_pw = ""
        print("\n--- Auto mode defaults ---")
        print(f"  Poll interval: {poll}s")
        print(f"  Battery alert threshold: {threshold}%")
        print("  Telegram alerts: disabled")
        print("  Home Assistant: disabled")
    else:
        poll = input("\nPoll interval in seconds [70]: ").strip() or "70"
        threshold = input("Low battery alert threshold % [20]: ").strip() or "20"

        print("\n--- Telegram Alerts (optional) ---")
        tg_enabled = input("Enable Telegram alerts? [y/N]: ").strip().lower() == "y"
        tg_token = tg_chat = ""
        if tg_enabled:
            tg_token = input("Bot token: ").strip()
            tg_chat = input("Chat ID: ").strip()

        print("\n--- Home Assistant (optional) ---")
        ha_enabled = input("Enable Home Assistant MQTT bridge? [y/N]: ").strip().lower() == "y"
        ha_host = ha_user = ha_pw = ""
        if ha_enabled:
            ha_host = input("HA MQTT broker host [localhost]: ").strip() or "localhost"
            ha_user = input("HA MQTT username (optional): ").strip()
            ha_pw = getpass.getpass("HA MQTT password (optional): ").strip()

    config = {
        "email": email,
        "password": password,
        "region": region,
        "devices": devices,
        "poll_interval": int(poll),
        "alerts": {
            "low_battery_percent": int(threshold),
            "cooldown_minutes": 30,
            "telegram": {"enabled": tg_enabled, "bot_token": tg_token, "chat_id": tg_chat},
            "ntfy": {"enabled": False, "url": ""},
            "webhook": {"enabled": False, "url": ""},
        },
        "homeassistant": {
            "enabled": ha_enabled,
            "mqtt_host": ha_host or "localhost",
            "mqtt_port": 1883,
            "mqtt_user": ha_user,
            "mqtt_password": ha_pw,
            "discovery_prefix": "homeassistant",
        },
        "rules": [
            {
                "name": "Low battery — turn off AC",
                "condition": {"battery_below": 10},
                "action": {"set_ac": False},
                "cooldown_minutes": 30,
                "_comment": "Example rule. Edit or remove as needed.",
            },
        ],
        "restore_outputs_after_shutdown": {
            "enabled": False,
            "shutdown_threshold_pct": 10,
            "shutdown_threshold_voltage": None,
            "minimum_offline_seconds": 120,
            "retry_interval_seconds": 30,
            "retry_timeout_seconds": 600,
            "snapshot_max_age_seconds": 86400,
            "_comment": (
                "When a device transitions offline at low SoC or low voltage, snapshot "
                "AC/DC switch state. When it later comes back online (e.g. mains "
                "restored after a low-battery shutdown), restore the previous switches. "
                "Set shutdown_threshold_voltage to a pack voltage such as 48.0 if SoC "
                "drift makes percentage unreliable near empty. See issue #59/#57. "
                "Set enabled: true to opt in; defaults are conservative."
            ),
        },
    }

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    _secure_config_permissions(CONFIG_PATH)

    print(f"\n✅ Config saved to {CONFIG_PATH}")

    # --- Systemd service installation (optional) ---
    if not auto:
        _setup_systemd_service()

        print("\nRun 'pecron-monitor' to start monitoring!")
        print("Run 'pecron-monitor --ac on' to test AC control!")
    else:
        print("\n✅ Auto setup complete!")


def _setup_systemd_service():
    """Optionally generate and install a systemd service file."""
    # Only offer on Linux with systemd
    if not shutil.which("systemctl"):
        return

    print("\n--- Systemd Service (optional) ---")
    install_service = (
        input("Install as a systemd service (auto-start on boot)? [y/N]: ").strip().lower() == "y"
    )
    if not install_service:
        return

    # Detect sensible defaults
    repo_dir = Path(__file__).parent.resolve()
    current_user = getpass.getuser()
    run_sh = repo_dir / "run.sh"

    print(f"\n  Detected install path: {repo_dir}")
    print(f"  Detected user: {current_user}")

    custom_user = input(f"  Run as user [{current_user}]: ").strip() or current_user
    custom_dir = input(f"  Install path [{repo_dir}]: ").strip() or str(repo_dir)
    custom_dir = str(Path(custom_dir).resolve())

    service_content = f"""[Unit]
Description=Pecron Battery Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={custom_user}
WorkingDirectory={custom_dir}
ExecStart={custom_dir}/run.sh
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    # Make sure run.sh exists and is executable
    if not run_sh.exists():
        print(f"  ⚠️  run.sh not found at {run_sh} — skipping service install.")
        print("  Make sure run.sh is in the repo root directory.")
        return
    if not os.access(run_sh, os.X_OK):
        os.chmod(run_sh, 0o755)

    # Write to a temp location first, then copy with sudo
    generated_path = repo_dir / "pecron-monitor.service.generated"
    with open(generated_path, "w") as f:
        f.write(service_content)

    print("\n  Generated service file:")
    print(f"    User: {custom_user}")
    print(f"    WorkingDirectory: {custom_dir}")
    print(f"    ExecStart: {custom_dir}/run.sh")

    do_install = (
        input("\n  Install to /etc/systemd/system/? (requires sudo) [Y/n]: ").strip().lower() != "n"
    )
    if not do_install:
        print(f"  Service file saved to: {generated_path}")
        print("  To install manually:")
        print(f"    sudo cp {generated_path} /etc/systemd/system/pecron-monitor.service")
        print("    sudo systemctl daemon-reload")
        print("    sudo systemctl enable --now pecron-monitor")
        return

    try:
        subprocess.run(
            ["sudo", "cp", str(generated_path), "/etc/systemd/system/pecron-monitor.service"],
            check=True,
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)

        enable_now = input("  Enable and start now? [Y/n]: ").strip().lower() != "n"
        if enable_now:
            subprocess.run(["sudo", "systemctl", "enable", "--now", "pecron-monitor"], check=True)
            print("  ✅ Service installed and running!")
            print("  Check status: sudo systemctl status pecron-monitor")
            print("  View logs:    sudo journalctl -u pecron-monitor -f")
        else:
            subprocess.run(["sudo", "systemctl", "enable", "pecron-monitor"], check=True)
            print("  ✅ Service installed and enabled (will start on next boot).")
            print("  To start now: sudo systemctl start pecron-monitor")
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Installation failed: {e}")
        print(f"  Service file saved to: {generated_path}")
        print(
            f"  Install manually with: sudo cp {generated_path} /etc/systemd/system/pecron-monitor.service"
        )
    finally:
        # Clean up generated file if it was installed
        if Path("/etc/systemd/system/pecron-monitor.service").exists():
            generated_path.unlink(missing_ok=True)
