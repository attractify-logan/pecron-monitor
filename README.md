# Pecron Battery Monitor

**v0.7.17** · [Changelog](CHANGELOG.md) · [Latest release](https://github.com/attractify-logan/pecron-monitor/releases/latest) · [Project board](https://github.com/users/attractify-logan/projects/1)

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
pip3 install -e .
pecron-monitor --setup
```

The setup wizard walks you through login, device discovery, and LAN/BLE scanning. You'll need your Pecron account email/password and your **Device Key** (found in the Pecron app under Device → ⚙️ → Device Info — 12 hex characters like `AABBCCDDEEFF`).

## Usage

```bash
# One-shot status check
pecron-monitor --status
python3 pecron_monitor.py --status

# Continuous monitoring (runs forever, polls every 70s by default)
pecron-monitor
python3 pecron_monitor.py

# Control outputs
pecron-monitor --ac on
pecron-monitor --dc off

# Offline mode (no internet, uses local WiFi/BLE only)
pecron-monitor --local

# Online only mode (uses internet, MQTT/REST transports)
pecron-monitor --nolocal

# REST only mode (uses internet, REST transport only)
pecron-monitor --rest-only

# See all available controls for your model
pecron-monitor --controls

# Set value for a specific control
pecron-monitor --control <name> <value>

# Raw JSON dump (debugging)
pecron-monitor --raw

# Diagnostics
pecron-monitor --diagnose --verbose

# Probe a control's supported values (tries 0,1,2,... until readback no longer matches)
pecron-monitor --probe-control ac_discharge_power_hm --probe-max 40
# Start probing at a custom value
pecron-monitor --probe-control ac_discharge_power_hm --probe-min 10 --probe-max 40
# Probe over cloud only (skip local TCP/BLE setup)
pecron-monitor --probe-control ac_discharge_power_hm --probe-max 40 --nolocal
```

### Example Output

```
==================================================
Device: AABBCCDDEEFF
Connection: LOCAL TCP
Data Quality: ✅ Full telemetry
==================================================
Status:        Shut Down (0)
Battery:       50%
Voltage:       52.1V
Temperature:   35°C
Discharge time:2h 19m
Charge time:   56h 6m
Net Drain:     226.5W
Total Input:   0W
Total Output:  198W
AC Output:     198W @ 122V
DC Output:     0W
AC Input:      0W
DC Input:      0W
AC Switch:     ON
DC Switch:     OFF
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

poll_interval: 70   # seconds; 63 is the hard floor, 70 is the recommended default. See note below.

alerts:
  low_battery_percent: 20
  cooldown_minutes: 30
  telegram:
    enabled: true
    bot_token: "your-bot-token"
    chat_id: "your-chat-id"

rule_state:
  initial_state: normal
  # path: ~/.pecron-monitor-rules.json

rules:
  - name: "Low battery — turn off AC"
    condition:
      battery_below: 10
      state: normal        # Optional: only fires while persisted rule state is "normal"
    action:
      set_ac: false
    cooldown_minutes: 30

restore_outputs_after_shutdown:
  enabled: false
  shutdown_threshold_pct: 10
  shutdown_threshold_voltage: null  # Optional, e.g. 48.0 for voltage-based low-battery detection
  minimum_offline_seconds: 120
  retry_interval_seconds: 30
  retry_timeout_seconds: 600
  snapshot_max_age_seconds: 86400
```

> **`poll_interval` floor.** Pecron's cloud rate-limits per-account at roughly 1280 polls/day (issue #29). The monitor refuses to use cloud polling below 63s and warns between 63 and 69s. Below 63s the cap trips daily around 23:00 UTC with `code 4026 'Insufficient resources'`. The default of 70s leaves comfortable margin. Local/offline mode (`--local`) is not subject to this cloud quota and may use faster polling for LAN/BLE monitoring. Raise cloud polling further if you're seeing 4026 in your logs.

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
| `voltage_below` | `48.0` — fires at or below 48.0V |
| `voltage_above` | `54.0` — fires at or above 54.0V |
| `input_power_below` | `5` — no solar/charging input |
| `input_power_above` | `100` — charging detected |
| `output_power_below` | `2000` — load is light |
| `output_power_above` | `2500` — heavy load (e.g. avoid charging into an overload) |
| `schedule` | `"00:00"` — fires only on an exact `HH:MM` poll |
| `schedule_between` | `["17:00", "21:00"]` — within a daily window; wraps midnight (`["22:00","06:00"]`) |
| `init` | `true` — fires once when the service starts |
| `state` | `"normal"` — only in this state; or `{mode: peak, charge: armed}` to require named variables |
| `states` | `["normal", "peak"]` — any listed state; or `{mode: [peak, shoulder]}` per named variable |

**Multiple conditions in one rule are ANDed** — every trigger key present must
hold for the rule to fire (e.g. `voltage_below` + `output_power_below` to charge
only when low *and* not under heavy load). `state`/`states` are separate gates
checked first. A rule with only a `state`/`states` gate and no trigger never
fires. Prefer `schedule_between` over `schedule`: an exact `schedule` only fires
if a poll lands on that precise minute, which a multi-minute `poll_interval` can
skip.

Actions: `set_ac`, `set_dc`, `set_ups` (true/false), `set_state`, `run_command`

Rule state is persisted at `~/.pecron-monitor-rules.json` (override with
`rule_state.path`) and survives service restarts. The simplest form is a single
string state: set `rule_state.initial_state: normal`, gate rules with
`state`/`states`, and transition with `set_state: low`.

For independent concerns, use **named state variables**: make `initial_state` a
map (e.g. `{mode: off, charge: idle}`), gate with `state: {mode: peak}` /
`states: {mode: [peak, shoulder]}` (every named variable must match), and update
selected variables with `set_state: {mode: peak}` (others are left unchanged).
Legacy single-string state lives under the reserved `default` variable, so old
configs and state files keep working unchanged.

`run_command` executes an external command without a shell. Provide either an
argv list or a string that can be split like a shell command. The monitor sends
JSON on stdin containing the rule name, current state, device key, target device
key, battery percent, voltage, and raw telemetry. Use `timeout_seconds` on the
action to override the 30s default.

### Restore Outputs After Low-Battery Shutdown

`restore_outputs_after_shutdown` is opt-in. When enabled, the monitor snapshots
AC/DC switch state if the device goes offline at or below
`shutdown_threshold_pct`. If SoC drifts on your LFP pack, set
`shutdown_threshold_voltage` as an additional low-battery detector; either the
percentage threshold or the voltage threshold can trigger the snapshot. When the
unit later comes back online after `minimum_offline_seconds`, the monitor retries
the saved AC/DC commands until telemetry confirms the switches match or
`retry_timeout_seconds` elapses.

## Offline Mode

After running `--setup` once (needs internet to fetch encryption key), everything works offline:

```bash
pecron-monitor --local    # Force offline
pecron-monitor            # Auto-fallback if cloud unavailable
```

Works over WiFi TCP and/or Bluetooth. All monitoring, controls, and automations function offline. Only alerts that need internet (Telegram, ntfy, webhooks) won't fire.

## Docker

Build and run locally with Docker Compose:

```bash
docker compose up -d --build
```

The compose file expects `config.yaml` in the repo directory and mounts it read-only at `/config/config.yaml`. It uses host networking so LAN/TCP discovery and Home Assistant MQTT work like a normal host install on Linux. Docker Desktop does not provide equivalent host networking for LAN discovery; set `lan_ip` in `config.yaml` if discovery cannot see the device.

One-shot Docker run:

```bash
docker build -t pecron-monitor:local .
docker run --rm --network host \\
  -v "$PWD/config.yaml:/config/config.yaml:ro" \\
  -v pecron-monitor-data:/data \\
  pecron-monitor:local pecron-monitor --config /config/config.yaml --status
```

## Home Assistant

```yaml
# Add to config.yaml
homeassistant:
  enabled: true
  mqtt_host: "192.168.1.100"
  mqtt_port: 1883
  mqtt_user: "user"
  mqtt_password: "pass"
  discovery_prefix: "homeassistant"
  clear_discovery_on_startup: true
```

Run with `--homeassistant` or just start normally (auto-detects if enabled). Your Pecron appears in HA with battery sensors, power sensors, remaining time, and AC/DC/UPS switches.

By default, the bridge clears each current retained discovery topic before
republishing it on startup. This makes Home Assistant pick up discovery payload
field changes after a service restart instead of requiring a manual MQTT
integration reload. Set `clear_discovery_on_startup: false` if you need the old
publish-only behavior.

### Energy Dashboard

The Pecron's input/output channels are published as **power** sensors (Watts):
AC Input Power, AC Output Power, DC Input Power, DC Output Power. Home
Assistant's **Energy Dashboard only accepts energy sensors (kWh)** — it cannot
add a Watt sensor directly, which is why these don't show up in the dashboard's
device picker even though the values look correct.

The device firmware does not report cumulative kWh counters for these channels
(only PV models expose a `Total PV Energy` sensor), so the fix is to let Home
Assistant integrate power over time with its built-in **Riemann sum integral**
helper. Each power sensor declares `state_class: measurement`, so HA records
long-term statistics for it and can integrate it cleanly.

**UI (easiest):** Settings → Devices & Services → **Helpers** → Create Helper →
**Integration - Riemann sum integral**. Pick a Pecron power sensor as the source,
set **Metric prefix = `k` (kilo)** and **Time unit = `h` (hours)**. The result is
a kWh sensor you can add under Settings → Dashboards → **Energy**.

**YAML** equivalent (use your own entity IDs):

```yaml
sensor:
  - platform: integration
    source: sensor.pecron_e1500lfp_ac_input_power   # grid charging in
    name: Pecron AC Input Energy
    unit_prefix: k       # -> kWh
    unit_time: h
    method: left
    max_sub_interval: "00:05:00"   # advance even when power is steady
  - platform: integration
    source: sensor.pecron_e1500lfp_ac_output_power  # AC load out
    name: Pecron AC Output Energy
    unit_prefix: k
    unit_time: h
    method: left
    max_sub_interval: "00:05:00"
  # ...repeat for DC Input Power (solar) and DC Output Power
```

Then in the Energy Dashboard add the AC/DC **Output** energy sensors under
"Individual devices", and the **Input** energy sensors (e.g. solar) under "Solar
panels" or "Grid consumption" as appropriate. Note that resolution is bounded by
`poll_interval` (the bridge only publishes new values that often), so the energy
totals are approximate, not revenue-grade.

## Running as a Service

The setup wizard (`--setup`) offers to install a systemd service automatically at the end — it detects your user and install path and handles everything.

To install manually instead:

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

## Related Projects

- [**jsight/unofficial-pecron-api**](https://github.com/jsight/unofficial-pecron-api): a clean Python library for the Pecron cloud REST API, published to PyPI (`pip install unofficial-pecron-api`). If you only need cloud access from Python (no local TCP, BLE, MQTT streaming, or Home Assistant integration) and want a well-typed SDK with dataclass models and a focused CLI, it's a great choice. Separate reverse-engineering effort. Credit for a few of the Pecron API quirks documented [here](docs/known-pecron-api-quirks.md) belongs to that project.

## Known Pecron API Quirks

Bugs and oddities in Pecron's cloud API / device firmware that affect integrations. See [docs/known-pecron-api-quirks.md](docs/known-pecron-api-quirks.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT
