"""
LAN discovery functions for pecron-monitor.

Provides network scanning and device discovery on the local network.
"""

import logging
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("pecron")


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
    """Interactive LAN setup: scan network, auto-match devices via auth_key handshake.

    Returns the modified devices list with lan_ip and auth_key added.
    """
    from local_transport import get_auth_key

    # Ensure all devices have auth_keys first (needed for handshake matching)
    for device in devices:
        dk = device["device_key"]
        if not device.get("auth_key"):
            try:
                print(
                    f"  Fetching encryption key for {device.get('name', dk)}...", end="", flush=True
                )
                auth_key = get_auth_key(token, region, device["product_key"], dk)
                device["auth_key"] = auth_key
                print(" ✅")
            except Exception as e:
                print(f" ❌ ({e})")

    # Auto-discover: scan for port 6607, then handshake to identify each device
    print("  Scanning local network for Pecron devices...")
    discovered = discover_devices(devices, timeout=0.5)

    if discovered:
        for dk, ip in discovered.items():
            for d in devices:
                if d["device_key"] == dk:
                    d["lan_ip"] = ip
                    print(f"  ✅ {d.get('name', dk)} → {ip} (auto-matched via handshake)")
                    break
    else:
        print("  No Pecron devices found on LAN via auto-discovery.")

    # Offer manual IP entry for any devices not found
    for device in devices:
        if device.get("lan_ip"):
            continue
        manual_ip = input(
            f"  Enter LAN IP for {device.get('name', device['device_key'])} (or Enter to skip): "
        ).strip()
        if manual_ip:
            device["lan_ip"] = manual_ip

    unmatched = [d for d in devices if not d.get("lan_ip")]
    if unmatched:
        names = ", ".join(d.get("name", d["device_key"]) for d in unmatched)
        print(f"  ℹ️  Not found on LAN: {names}")
        print("     These will use cloud MQTT only (or configure lan_ip in config.yaml later)")

    print("  LAN discovery complete!")
    return devices


def discover_devices(devices_config: list, timeout: float = 0.5) -> dict:
    """Auto-discover Pecron devices on LAN by scanning for port 6607 and trying handshakes.

    Args:
        devices_config: List of device dicts, each with 'device_key' and 'auth_key'
        timeout: Socket timeout for port scan and handshake attempts

    Returns:
        Dict mapping device_key → IP for discovered devices
    """
    from local_transport import LocalTransport
    import ipaddress

    # Auto-detect subnet
    subnet = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        subnet = str(net)
    except Exception:
        subnet = "192.168.1.0/24"

    log.info("Auto-discovery: scanning %s for Pecron devices (port 6607)...", subnet)

    # Parallel port scan for 6607
    net = ipaddress.IPv4Network(subnet, strict=False)
    found_ips = []

    def check_port(ip: str) -> str:
        """Check if port 6607 is open on this IP."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(max(timeout * 2, 1.0))  # More generous timeout for port scan
            result = sock.connect_ex((ip, 6607))
            sock.close()
            if result == 0:
                return ip
        except Exception as e:
            log.debug("Port check failed for %s: %s", ip, e)
        return None

    # Scan all hosts in parallel (fewer workers to avoid overwhelming network)
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(check_port, str(host)): str(host) for host in net.hosts()}
        for future in as_completed(futures):
            result = future.result()
            if result:
                found_ips.append(result)
                log.debug("Found port 6607 open at %s", result)

    if not found_ips:
        log.warning("No devices with port 6607 found on %s", subnet)
        return {}

    log.info("Found %d device(s) with port 6607 open: %s", len(found_ips), ", ".join(found_ips))

    # Try handshake with each device's auth_key to identify which IP belongs to which device
    # Strategy: for each IP, try all auth_keys until one succeeds
    discovered = {}
    ip_to_device = {}  # Track which device matched which IP

    for ip in found_ips:
        if ip in ip_to_device:
            continue  # Already identified

        for device in devices_config:
            dk = device.get("device_key")
            auth_key = device.get("auth_key")

            if not dk or not auth_key:
                continue

            if dk in discovered:
                # This device already found at a different IP
                continue

            log.debug("Trying handshake for device %s at %s...", dk, ip)
            try:
                import time

                time.sleep(0.1)  # Small delay between attempts to avoid overwhelming device
                transport = LocalTransport(ip, auth_key, timeout=3.0)
                if transport.connect():
                    log.info("✅ Discovered %s at %s (handshake successful)", dk, ip)
                    discovered[dk] = ip
                    ip_to_device[ip] = dk
                    transport.disconnect()
                    break  # Found the right device for this IP
                transport.disconnect()
            except Exception as e:
                log.debug("Handshake failed for %s at %s: %s", dk, ip, e)

    if not discovered:
        log.warning("No devices identified (handshake failed with all auth_keys)")
    else:
        log.info("Auto-discovery complete: found %d device(s)", len(discovered))

    return discovered
