"""
LAN discovery functions for pecron-monitor.

Provides network scanning and device discovery on the local network.
"""

import socket


def _scan_lan_for_pecron(subnet: str = None, timeout: float = 0.3) -> list:
    """Scan local network for devices with TCP port 6607 open."""
    import ipaddress
    results = []
    if not subnet:
        # Try to detect subnet from default interface
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            # Assume /24
            net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
            subnet = str(net)
        except Exception:
            subnet = "192.168.1.0/24"

    print(f"  Scanning {subnet} for Pecron devices (port 6607)...")
    net = ipaddress.IPv4Network(subnet, strict=False)
    for host in net.hosts():
        ip = str(host)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            if sock.connect_ex((ip, 6607)) == 0:
                results.append(ip)
                print(f"  Found: {ip}")
            sock.close()
        except Exception:
            pass
    return results


def _setup_lan_discovery(devices: list, token: str, region: dict) -> list:
    """Interactive LAN setup: scan network, match devices, fetch auth keys.

    Returns the modified devices list with lan_ip and auth_key added.
    """
    from local_transport import get_auth_key

    found_ips = _scan_lan_for_pecron()

    if not found_ips:
        print("  No Pecron devices found on LAN.")
        manual_ip = input("  Enter device IP manually (or press Enter to skip): ").strip()
        if manual_ip:
            found_ips = [manual_ip]
        else:
            return devices

    for device in devices:
        dk = device["device_key"]
        if len(found_ips) == 1:
            ip = found_ips[0]
            print(f"  Assigning {ip} to {device.get('name', dk)}")
        else:
            print(f"\n  Multiple Pecron devices found. Which IP is {device.get('name', dk)}?")
            for i, ip in enumerate(found_ips):
                print(f"    {i + 1}. {ip}")
            choice = input(f"  Choose [1-{len(found_ips)}]: ").strip()
            try:
                ip = found_ips[int(choice) - 1]
            except (ValueError, IndexError):
                print("  Skipping.")
                continue

        device["lan_ip"] = ip

        # Fetch and cache auth key
        try:
            print(f"  Fetching encryption key for {dk}...", end="", flush=True)
            auth_key = get_auth_key(token, region, device["product_key"], dk)
            device["auth_key"] = auth_key
            print(f" ✅")
        except Exception as e:
            print(f" ❌ ({e})")
            print("  Local monitoring will fetch the key on next startup (requires internet).")

    print("  LAN configuration complete!")
    return devices
