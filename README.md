# Pecron Battery Monitor

**v0.5.5** · [Changelog](CHANGELOG.md)

Monitor and control Pecron portable power stations from the command line — no phone app required.

**Three ways to connect**, with automatic fallback:

| | Bluetooth (BLE) | WiFi (TCP) | Cloud (MQTT) |
|---|---|---|---|
| Internet needed? | ❌ | ❌ | ✅ |
| WiFi needed? | ❌ | ✅ | ✅ |
| Range | ~30 ft | LAN | Anywhere |

Works with **any Pecron model** that uses the Pecron app (E300LFP through F5000LFP — [full list](#supported-models)). Runs on Raspberry Pi, Linux, Mac, or anything with Python 3.9+.

## What You Can Do

- **24/7 monitoring** — battery %, voltage, temperature, power in/out
- **Remote control** — turn AC/DC on/off from the command line
- **Alerts** — Telegram, ntfy, or webhook when battery gets low
- **Automation** — rules like "turn off AC below 10%"
- **Home Assistant** — auto-discovered MQTT sensors and switches
- **Fully offline** — after one-time setup, no internet needed

## Quick Start

```bash
git clone https://github.com/attractify-logan/pecron-monitor.git
cd pecron-monitor
pip3 install -r requirements.txt
python3 pecron_monitor.py --setup
```

The setup wizard walks you through login, device discovery, and LAN/BLE scanning. You'll need your Pecron account email/password and your **Device Key** (found in the Pecron app under Device → ⚙️ → Device Info — 12 hex characters like `AABBCCDDEEFF`).

## Usage

```bash
# One-shot status check
python3 pecron_monitor.py --status

# Continuous monitoring (runs forever, polls every 60s)
python3 pecron_monitor.py

# Control outputs
python3 pecron_monitor.py --ac on
python3 pecron_monitor.py --dc off

# Offline mode (no internet, uses local WiFi/BLE only)
python3 pecron_monitor.py --local

# See all available controls for your model
python3 pecron_monitor.py --controls

# Raw JSON dump (debugging)
python3 pecron_monitor.py --raw

# Diagnostics
python3 pecron_monitor.py --diagnose -v
```

### Example Output

```
==================================================
Device: AABBCCDDEEFF
Connection: LOCAL TCP
==================================================
Battery:       73%
Voltage:       51.8V
Temperature:   24°C
Remaining:     8h 42m
Total Input:   145W
Total Output:  0W
AC Output:     0W @ 120V
DC Output:     0W
AC Switch:     OFF
DC Switch:     ON
UPS Mode:      OFF
```

## Configuration

Everything lives in `config.yaml` (created by `--setup`):

```yaml
email: "you@email.com"
password: "your-password"
region: "na"                    # na, eu, or cn

devices:
  - product_key: "p11u2b"
    device_key: "AABBCCDDEEFF"
    name: "E1500LFP"
    lan_ip: "192.168.1.100"     # For WiFi TCP (auto-detected by setup)
    auth_key: "base64key=="     # Fetched from cloud once, cached forever

poll_interval: 60

alerts:
  low_battery_percent: 20
  cooldown_minutes: 30
  telegram:
    enabled: true
    bot_token: "your-bot-token"
    chat_id: "your-chat-id"

rules:
  - name: "Low battery — turn off AC"
    condition:
      battery_below: 10
    action:
      set_ac: false
    cooldown_minutes: 30
```

### Alert Options

| Method | Config key | Notes |
|--------|-----------|-------|
| Telegram | `alerts.telegram` | Needs bot token + chat ID ([setup guide](https://core.telegram.org/bots#how-do-i-create-a-bot)) |
| ntfy | `alerts.ntfy` | Just set `url` to your ntfy topic |
| Webhook | `alerts.webhook` | POSTs JSON to any URL |

### Automation Rules

| Condition | Example |
|-----------|---------|
| `battery_below` | `10` — fires at or below 10% |
| `battery_above` | `95` — fires at or above 95% |
| `input_power_below` | `5` — no solar/charging input |
| `input_power_above` | `100` — charging detected |
| `schedule` | `"00:00"` — time-based (24h format) |

Actions: `set_ac`, `set_dc`, `set_ups` (true/false)

## Offline Mode

After running `--setup` once (needs internet to fetch encryption key), everything works offline:

```bash
python3 pecron_monitor.py --local    # Force offline
python3 pecron_monitor.py            # Auto-fallback if cloud unavailable
```

Works over WiFi TCP and/or Bluetooth. All monitoring, controls, and automations function offline. Only alerts that need internet (Telegram, ntfy, webhooks) won't fire.

## Home Assistant

```yaml
# Add to config.yaml
homeassistant:
  enabled: true
  mqtt_host: "192.168.1.100"
  mqtt_port: 1883
  mqtt_user: "user"
  mqtt_password: "pass"
```

Run with `--homeassistant` or just start normally (auto-detects if enabled). Your Pecron appears in HA with battery sensors, power sensors, remaining time, and AC/DC/UPS switches.

## Running as a Service

```bash
# 1. Edit pecron-monitor.service — update User, WorkingDirectory, and ExecStart
#    to match your system (defaults assume user=pi at /home/pi/pecron-monitor)
nano pecron-monitor.service

# 2. Install and start
sudo cp pecron-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pecron-monitor
```

> **Tip:** The service file uses `run.sh` as a wrapper so that `git checkout` / `git pull`
> operations won't break the running service. If you update the service file itself,
> re-copy it and run `sudo systemctl daemon-reload`.

## Bluetooth (BLE) Setup

Optional — only needed if you want Bluetooth monitoring (no WiFi required):

```bash
pip3 install bleak
```

Most laptops and Raspberry Pi 3/4/5 have BLE built in. Desktop PCs may need a USB BLE dongle (~$10). The setup wizard auto-discovers Pecron BLE devices.

## Project Structure

```
pecron_monitor.py   — CLI entry point
monitor.py          — Core PecronMonitor class
ha_bridge.py        — Home Assistant MQTT bridge
cloud_api.py        — Cloud auth & device discovery
local_transport.py  — Local TCP/WiFi encrypted transport
protocol.py         — TTLV packet encoding
constants.py        — Regions, products, sensor mappings
helpers.py          — Utility functions
lan_scan.py         — LAN device scanning
setup_wizard.py     — Interactive setup
```

## Supported Models

| Model | Key | | Model | Key |
|-------|-----|-|-------|-----|
| E300LFP | p11u2Q | | E2400LFP | p11tf9 |
| C300LFP Mini | p11uXh | | E2400LFP ADJ | p11vB4 |
| E500LFP | p11uFC | | E3600 | p11tUC |
| E600LFP | p11umP | | E3600LFP | p11wV4 |
| E800LFP | p11uXR | | E3800LFP | p11uJn |
| E1000LFP | p11vxg | | F1000LFP | p11vWw |
| E1500LFP | p11u2b | | F3000LFP | p11uAG |
| E2000LFP | p11usc | | F5000LFP | p11vwW |
| E2200LFP | p11t8R | | WB12200 | p11vGo |

Don't see yours? It probably still works — `--setup` checks all known product keys automatically.

## Troubleshooting

**"Login failed"** — Check email/password. Google/Apple sign-in users need to set a password in the Pecron app first.

**"No data received"** — Device needs WiFi. Open the Pecron app briefly to wake the WiFi module, then retry.

**"Cannot run in offline mode"** — Run `--setup` first (needs internet once to fetch encryption key).

**Local TCP not connecting** — Verify `lan_ip` is correct and port 6607 is open: `nc -zv 192.168.1.100 6607`

**Wrong model name** — Cosmetic issue from Pecron's cloud catalog. Run `--diagnose -v` if data isn't working.

## Security

- Credentials stored locally in `config.yaml` only — `chmod 600 config.yaml`
- No telemetry, no tracking
- Password is AES-encrypted before transmission (same as official app)
- Tokens expire every 2 hours and auto-refresh

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT
