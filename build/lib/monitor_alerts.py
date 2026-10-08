"""Low-battery alert policy for :class:`monitor.PecronMonitor`."""

import json
import logging
import time
import urllib.parse
import urllib.request


log = logging.getLogger("pecron")


class MonitorAlertsMixin:
    def _check_alerts(self, device_key, battery_pct, voltage, remain):
        alerts = self.config.get("alerts", {})
        threshold = alerts.get("low_battery_percent", 20)
        cooldown = alerts.get("cooldown_minutes", 30) * 60
        if battery_pct >= 0 and battery_pct <= threshold:
            now = time.time()
            last = self.last_alert.get(device_key, 0)
            if now - last > cooldown:
                self.last_alert[device_key] = now
                self._send_alert(device_key, battery_pct, voltage, remain)

    def _get_device_name(self, device_key):
        """Get human-readable device name from device_key."""
        for dev in self.devices:
            if dev.get("device_key") == device_key:
                return dev.get("device_name", dev.get("name", device_key))
        return device_key

    def _send_alert(self, device_key, battery_pct, voltage, remain_min):
        device_name = self._get_device_name(device_key)
        msg = (
            f"⚠️ Pecron Low Battery Alert\n"
            f"Device: {device_name}\n"
            f"Battery: {battery_pct}%\nVoltage: {voltage:.1f}V\n"
            f"Remaining: {remain_min // 60}h {remain_min % 60}m"
        )
        log.warning(msg)
        alerts = self.config.get("alerts", {})

        tg = alerts.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            try:
                url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
                data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": msg}).encode()
                urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
            except Exception as e:
                log.error("Telegram alert failed: %s", e)

        ntfy = alerts.get("ntfy", {})
        if ntfy.get("enabled") and ntfy.get("url"):
            try:
                req = urllib.request.Request(
                    ntfy["url"],
                    data=msg.encode(),
                    headers={"Title": f"Pecron Battery {battery_pct}%"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.error("ntfy alert failed: %s", e)

        wh = alerts.get("webhook", {})
        if wh.get("enabled") and wh.get("url"):
            try:
                payload = json.dumps(
                    {
                        "battery_percent": battery_pct,
                        "voltage": voltage,
                        "remain_minutes": remain_min,
                        "device_key": device_key,
                        "message": msg,
                    }
                ).encode()
                req = urllib.request.Request(
                    wh["url"], data=payload, headers={"Content-Type": "application/json"}
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.error("Webhook alert failed: %s", e)
