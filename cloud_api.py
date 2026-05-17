"""
Cloud API functions for pecron-monitor.

Handles authentication, device discovery, and REST API queries to the
Pecron/Quectel cloud backend.
"""

import base64
import hashlib
import json
import logging
import secrets
import string
import urllib.parse
import urllib.request

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from constants import DEFAULT_CONTROLS

log = logging.getLogger("pecron")


# ===========================================================================
# Authentication
# ===========================================================================


def _make_auth_params(email: str, password: str, region: dict) -> dict:
    rand = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    md5 = hashlib.md5(rand.encode()).hexdigest().upper()
    aes_key = md5[8:24]
    iv = aes_key[8:16] + aes_key[0:8]
    cipher = AES.new(aes_key.encode(), AES.MODE_CBC, iv.encode())
    enc_pwd = base64.b64encode(cipher.encrypt(pad(password.encode(), 16))).decode()
    sig_input = email + enc_pwd + rand + region["user_domain_secret"]
    signature = hashlib.sha256(sig_input.encode()).hexdigest()
    return {
        "email": email,
        "pwd": enc_pwd,
        "random": rand,
        "userDomain": region["user_domain"],
        "signature": signature,
    }


def _do_login(email: str, password: str, region: dict) -> dict:
    """Attempt one login against the given region config."""
    params = _make_auth_params(email, password, region)
    url = region["base_url"] + "/v2/enduser/enduserapi/emailPwdLogin"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if body.get("code") != 200:
        raise RuntimeError(f"Login failed (code {body.get('code')}): {body.get('msg', body)}")
    token_data = body["data"]["accessToken"]
    token = token_data["token"]
    jwt_parts = token.split(".")
    payload_b64 = jwt_parts[1] + "=" * (4 - len(jwt_parts[1]) % 4)
    jwt_payload = json.loads(base64.b64decode(payload_b64))
    return {
        "token": token,
        "uid": jwt_payload["uid"],
        "expires_at": jwt_payload.get("exp", 0),
    }


_DOMAIN_RETRY_CODES = {
    5015,  # UserDomain does not exist.
    5031,  # Email address is not registered on this domain.
    5420,  # Signature verification failed for this domain secret.
}


def login(email: str, password: str, region: dict) -> dict:
    """Log in to the Pecron/Quectel cloud.

    Some NA accounts appear to be split across Pecron/Quectel domains. Try the
    configured primary domain first, then retry once with the fallback domain
    only for domain-related API failures.
    """
    try:
        return _do_login(email, password, region)
    except RuntimeError as primary_err:
        fallback_domain = region.get("user_domain_fallback")
        fallback_secret = region.get("user_domain_secret_fallback")
        if not fallback_domain or not fallback_secret:
            raise

        err = str(primary_err)
        if not any(f"code {code}" in err for code in _DOMAIN_RETRY_CODES):
            raise

        log.info(
            "Primary domain login failed (%s); retrying with fallback domain %s",
            primary_err,
            fallback_domain,
        )
        fallback_region = {
            **region,
            "user_domain": fallback_domain,
            "user_domain_secret": fallback_secret,
        }
        return _do_login(email, password, fallback_region)


# ===========================================================================
# Device discovery
# ===========================================================================


def get_product_catalog(token: str, region: dict) -> dict:
    url = region["base_url"] + "/v2/enduser/enduserapi/getProductList?pageNum=1&pageSize=100"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if body.get("code") != 200:
        return {}
    return {p["productKey"]: p["name"] for p in body.get("data", {}).get("list", [])}


def get_user_devices(token: str, region: dict) -> list:
    """Get devices already bound to the user's account (correct pk/dk pairs).

    This is the most reliable way to discover devices — returns the exact
    product_key and device_key that the cloud has on file, avoiding
    mismatches that cause 'device is not bound' (4007) errors.
    """
    url = region["base_url"] + "/v2/binding/enduserapi/userDeviceList"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") != 200:
            log.debug("userDeviceList failed: %s", body.get("msg", body))
            return []
        data = body.get("data", {})
        device_list = data.get("list", data) if isinstance(data, dict) else data
        if not isinstance(device_list, list):
            return []
        devices = []
        for d in device_list:
            pk = d.get("productKey", "")
            dk = d.get("deviceKey", "")
            name = d.get("productName", d.get("deviceName", "Unknown"))
            if pk and dk:
                devices.append({"product_key": pk, "device_key": dk, "name": name})
        return devices
    except Exception as e:
        log.debug("userDeviceList request failed: %s", e)
        return []


def get_product_tsl(token: str, region: dict, product_key: str) -> dict:
    """Fetch TSL (data model) for a product — gives us data point IDs."""
    url = region["base_url"] + f"/v2/binding/enduserapi/productTSL?pk={product_key}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") == 200:
            tsl = body["data"]
            controls = {}
            for prop in tsl.get("properties", []):
                dt = prop.get("dataType", {})
                dtype = dt.get("type", dt) if isinstance(dt, dict) else str(dt)
                access = prop.get("subType", prop.get("accessMode", "R"))
                controls[prop["code"]] = {
                    "id": prop["id"],
                    "type": dtype,
                    "desc": prop.get("name", prop["code"]),
                    "access": access,
                }
            return controls
    except Exception as e:
        log.debug("TSL fetch failed: %s", e)
    return {}


def verify_device(token: str, region: dict, product_key: str, device_key: str) -> dict:
    url = (
        region["base_url"]
        + f"/v2/binding/enduserapi/getDeviceBindingInfo?pk={product_key}&dk={device_key}"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") == 200:
            return body.get("data", {})
    except Exception:
        pass
    return {}


def get_device_online_status(token: str, region: dict, product_key: str, device_key: str) -> dict:
    """Check if device is online via cloud API."""
    url = (
        region["base_url"]
        + f"/v2/binding/enduserapi/getDeviceOnlineStatus?pk={product_key}&dk={device_key}"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") == 200:
            return body.get("data", {})
    except Exception as e:
        log.debug("Online status check failed: %s", e)
    return {}


def get_device_properties_rest(token: str, region: dict, pk: str, dk: str) -> dict:
    """Read device properties via REST API (same method as ha-pecron HACS addon).

    This is a reliable fallback when MQTT doesn't deliver data — it queries the
    cloud API directly for current device state.

    Returns a kv dict compatible with _process_data().
    """
    url = region["base_url"] + f"/v2/binding/enduserapi/getDeviceBusinessAttributes?pk={pk}&dk={dk}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    try:
        log.debug("REST URL: '%s'", url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("code") != 200:
            log.debug("REST properties failed: %s", body.get("msg", body))
            return {}

        log.debug("REST response body:\n%s", json.dumps(body, indent=2, ensure_ascii=False))
        tsl_info = body.get("data", {}).get("customizeTslInfo", [])
        if not tsl_info:
            log.debug("REST properties returned empty TSL info")
            return {}

        # Convert TSL info array to kv dict matching MQTT format
        kv = {}
        for item in tsl_info:
            code = item.get("resourceCode", "")
            value = item.get(
                "resourceValce", ""
            )  # Note: 'resourceValce' is correct per API response, not a typo
            dtype = item.get("dataType", "string")

            if value == "":
                continue

            # Convert value to native type according to dtype when possible
            def _convert_value(val, dtype):
                # If already native, return as-is
                if isinstance(val, (dict, list, bool, int, float)):
                    return val
                s = str(val)
                t = str(dtype).lower() if dtype is not None else ""
                try:
                    if t in ("int", "integer", "long", "number", "int32", "int64", "enum"):
                        return int(float(s))
                    if t in ("float", "double", "decimal"):
                        return float(s)
                    if t in ("bool", "boolean"):
                        low = s.strip().lower()
                        if low in ("true", "1", "on", "yes", "enabled"):
                            return True
                        if low in ("false", "0", "off", "no", "disabled"):
                            return False
                        # fallback: try numeric
                        try:
                            return bool(int(float(s)))
                        except Exception:
                            return s
                    if t in ("json", "struct", "object", "map", "array"):
                        try:
                            return json.loads(s)
                        except Exception:
                            return val
                    if t in ("text", "string"):
                        return s
                except Exception:
                    return val

                # Best-effort fallback: try int then float, else keep string
                try:
                    return int(s)
                except Exception:
                    try:
                        return float(s)
                    except Exception:
                        return s

            converted = _convert_value(value, dtype)
            kv[code] = converted
            log.debug(
                "code: %s value: %r (dtype=%s) -> kv[%s] = %r", code, value, dtype, code, converted
            )

        return kv
    except Exception as e:
        log.debug("REST properties request failed: %s", e)
        return {}


def set_device_property_rest(token: str, region: dict, pk: str, dk: str, properties: dict) -> bool:
    """Set one or more device properties via REST API.

    Args:
        token: Bearer token from login().
        region: Region dict from REGIONS.
        pk: Product key.
        dk: Device key.
        properties: Dict of property_code -> value, e.g. {"ac_switch_hm": True}.

    Returns:
        True if the cloud accepted the command (code 200), False otherwise.
    """
    url = region["base_url"] + "/v2/binding/enduserapi/batchControlDevice"
    data_list = [{code: value} for code, value in properties.items()]
    body = {
        "data": json.dumps(data_list),
        "deviceList": [{"productKey": pk, "deviceKey": dk}],
        "type": 2,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload)
    req.add_header("Authorization", token)
    req.add_header("Content-Type", "application/json")
    try:
        log.debug("set_device_property_rest: POST %s body=%s", url, body)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if result.get("code") != 200:
            log.warning("set_device_property_rest failed: %s", result.get("msg", result))
            return False
        log.debug("set_device_property_rest OK: %s", result.get("data"))
        return True
    except Exception as e:
        log.warning("set_device_property_rest request failed: %s", e)
        return False


def resolve_devices(config: dict, token: str, region: dict) -> list:
    catalog = get_product_catalog(token, region)
    devices = []
    configured = config.get("devices", [])
    if not configured:
        raise RuntimeError(
            "No devices configured. Run --setup or add devices to config.yaml.\n"
            "Find your device key in the Pecron app: Device → Settings → Device Info"
        )
    for d in configured:
        pk = d.get("product_key", "")
        dk = d.get("device_key", "")
        name = catalog.get(pk, d.get("name", "Unknown"))
        info = verify_device(token, region, pk, dk)
        if info:
            # Fetch TSL for this product
            tsl = get_product_tsl(token, region, pk)
            api_name = info.get("productName", name)
            devices.append(
                {
                    "product_key": pk,
                    "device_key": dk,
                    "device_name": api_name,
                    "product_name": api_name,
                    "controls": tsl or DEFAULT_CONTROLS,
                }
            )
            # Check online status
            online_info = get_device_online_status(token, region, pk, dk)
            online = online_info.get("online", online_info.get("value"))
            if online:
                log.info("  ✅ %s (pk=%s, dk=%s) — ONLINE", api_name, pk, dk)
            else:
                log.warning(
                    "  ⚠️  %s (pk=%s, dk=%s) — OFFLINE (device may not be connected to WiFi/internet)",
                    api_name,
                    pk,
                    dk,
                )
                log.warning(
                    "     MQTT monitoring will not receive data until the device is online."
                )
                log.warning("     Check: Is the device powered on? Is it connected to WiFi?")
                log.warning(
                    "     In the Pecron app, go to the device — if it shows 'offline', the device can't reach the cloud."
                )
            if api_name != name and name != "Unknown":
                log.info("     ℹ️  API identifies this as '%s' (config says '%s')", api_name, name)
        else:
            # Try to find correct pk from userDeviceList
            account_devs = get_user_devices(token, region)
            corrected = None
            for ad in account_devs:
                if ad["device_key"].upper() == dk.upper():
                    corrected = ad
                    break
            if corrected and corrected["product_key"] != pk:
                log.warning("  ⚠️  %s (%s) — wrong product_key in config (pk=%s)", name, dk, pk)
                log.info(
                    "     Auto-correcting to pk=%s (from account device list)",
                    corrected["product_key"],
                )
                pk = corrected["product_key"]
                tsl = get_product_tsl(token, region, pk)
                devices.append(
                    {
                        "product_key": pk,
                        "device_key": dk,
                        "device_name": corrected["name"],
                        "product_name": corrected["name"],
                        "controls": tsl or DEFAULT_CONTROLS,
                    }
                )
                log.info("  ✅ %s (pk=%s, dk=%s)", corrected["name"], pk, dk)
            else:
                log.warning("  ❌ %s (%s) — not found or not bound", name, dk)
                log.warning(
                    "     Check that your device_key is correct (Pecron app → Device → ⚙️ → Device Info → Device Key/Code)"
                )
                log.warning("     It should be 12 hex characters (your device's MAC address)")
    return devices
