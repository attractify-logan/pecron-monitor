# Pecron Battery Monitor

**v0.5.0** · [Changelog](CHANGELOG.md)

Real-time monitoring and control for Pecron portable power stations — no phone required.

**Three ways to connect — Bluetooth, WiFi, or Cloud — with automatic fallback.** Perfect for vanlife, off-grid, and anywhere you want monitoring without depending on Pecron's servers or even a WiFi network.

### 🔗 Connection Methods

| | **Bluetooth (BLE)** | **WiFi (TCP)** | **Cloud (MQTT)** |
|---|---|---|---|
| **Internet required?** | ❌ No | ❌ No | ✅ Yes |
| **WiFi required?** | ❌ No | ✅ Yes | ✅ Yes |
| **Range** | ~30 ft | Whole network | Anywhere |
| **Best for** | Vanlife, true off-grid | Home, RV with router | Remote monitoring |

The monitor tries **BLE → WiFi → Cloud** automatically and uses the first one that works.

> **🆕 Bluetooth (BLE) support** means you can monitor your Pecron with *zero network infrastructure* — no WiFi router, no internet, no hotspot. Just your computer and the battery within Bluetooth range. Ideal for vanlife and off-grid setups.

Works with **any** Pecron power station that uses the Pecron app: E300LFP, E500LFP, E600LFP, E800LFP, E1000LFP, E1500LFP, E2000LFP, E2200LFP, E2400LFP, E3600, E3600LFP, E3800LFP, F1000LFP, F3000LFP, F5000LFP, C300LFP Mini, WB12200, and future models.

Runs on a Raspberry Pi, Linux server, Mac, or any computer with Python.

---

## What Does This Do?

The Pecron app on your phone lets you check your battery and control it. This tool does the **same thing**, but from a computer — without needing your phone at all.

That means you can:
- **Monitor your battery 24/7** from a Raspberry Pi sitting in your closet
- **Get alerts** on Telegram or your phone when the battery is low
- **Turn AC/DC outputs on and off** from the command line
- **Set up automations** like "turn off AC when battery drops below 10%"
- **Integrate with Home Assistant** so your Pecron shows up as a smart home device
- **Work offline** — local WiFi monitoring with no internet required (cloud fallback when available)

---


## Local Monitoring (No Internet Required)

The monitor supports **three connection methods** (BLE → WiFi → Cloud) that automatically fall back to each other. See the [connection table above](#-connection-methods) for details.

### Quick Setup

Run `python pecron_monitor.py --setup` and the wizard will:
1. Scan your WiFi network for Pecron devices (TCP port 6607)
2. Scan Bluetooth for nearby Pecron devices
3. Fetch a one-time encryption key from the cloud (cached locally forever)

After initial setup, everything works offline.

### Manual Configuration

Add any combination of `lan_ip`, `ble_address`, or `ble: true` to your device in `config.yaml`:

```yaml
devices:
  - product_key: "p11u2b"
    device_key: "AABBCCDDEEFF"
    name: "E1500LFP"
    lan_ip: "192.168.1.100"           # WiFi TCP — device's local IP
    ble_address: "68:24:99:E3:FF:AA"  # Bluetooth — device's BLE MAC address
    ble: true                          # Or just 'true' to auto-scan by device key
    auth_key: "base64keyhere=="        # Optional — auto-fetched from cloud if missing
```

You only need `auth_key` once — it's fetched from the cloud on first run and cached forever. After that, WiFi TCP and Bluetooth work with zero internet.

### Bluetooth Setup

BLE requires the `bleak` Python library:

```bash
pip install bleak
```

Your computer needs a Bluetooth adapter (most laptops and Raspberry Pi 3/4/5 have one built in). Desktop PCs may need a USB BLE dongle (~$10).

Pecron devices advertise as `QUEC_BLE_XXXX` where `XXXX` matches the last 4 characters of your device key. The setup wizard finds this automatically.

### Finding Your Device's WiFi IP

Your Pecron listens on TCP port 6607 when connected to WiFi. To find it:
- Check your router's DHCP client list for a device with MAC starting with `68:24:99`
- Or run: `nmap -p 6607 192.168.1.0/24` (replace with your subnet)

---

## Offline / Local-Only Mode

You can run pecron-monitor **completely offline** — no internet connection required — using local TCP or Bluetooth. This is perfect for:

- 🔐 **Security** — Firewall your Pecron from the internet entirely
- 🚐 **Vanlife/Off-Grid** — Monitor without relying on cloud infrastructure
- ⚡ **Speed** — Local TCP is faster than cloud MQTT
- 🛡️ **Privacy** — No data leaves your local network

### How It Works

During setup (`--setup`), the wizard fetches a one-time encryption key (`auth_key`) from the cloud and saves it to `config.yaml`. After that, the key is cached forever and you never need internet again.

The script automatically detects offline capability: if all devices in your config have `lan_ip` or `ble_address` + `auth_key` + `product_key` + `device_key`, it will gracefully fall back to offline mode if cloud login fails.

### Quick Start

```bash
# 1. Run setup (requires internet once)
python pecron_monitor.py --setup

# The wizard will:
#   - Scan your WiFi network for Pecron devices (port 6607)
#   - Fetch the encryption key from cloud (cached forever)
#   - Save lan_ip and auth_key to config.yaml

# 2. Run in offline mode
python pecron_monitor.py --local

# Or just run normally — it will auto-fallback to offline if cloud is unavailable
python pecron_monitor.py
```

### What Gets Cached

When you run `--setup`, these fields are saved to `config.yaml` for each device:

```yaml
devices:
  - product_key: "p11u2b"
    device_key: "AABBCCDDEEFF"
    name: "E1500LFP"
    lan_ip: "192.168.1.100"         # Device's local IP (from LAN scan)
    auth_key: "base64encodedkey=="   # Encryption key (from cloud, cached forever)
    tsl_cache:                       # Controls metadata (from cloud, cached)
      ac_switch_hm:
        id: 40
        type: "BOOL"
        desc: "AC output"
      # ... etc
```

### Running Fully Offline

Once your config has these fields, you can:

1. **Firewall your Pecron** — Block internet access entirely
2. **Run without internet** — The monitor works offline:

```bash
python pecron_monitor.py --local
```

Or run normally and it will auto-detect offline mode if cloud login fails.

### What Works Offline

✅ **Full monitoring** — Battery%, voltage, temp, power in/out
✅ **Controls** — Turn AC/DC on/off, change settings
✅ **Automation rules** — Battery-level triggers, time-based actions
✅ **Home Assistant MQTT bridge** — Works if HA is on local network

### What Requires Internet

❌ **Alerts** — Telegram, ntfy, webhooks all need internet (will warn but not crash)
❌ **TSL updates** — If Pecron releases a new firmware with new controls, you'll need to re-run `--setup` to fetch updated metadata

### Offline Mode Logging

When running in offline or local mode, all log messages clearly show the data source:

```
🔋 73% | 51.8V | 24°C | ⚡ In:145W Out:0W | ⏱ 8h 42m [via LOCAL TCP]
```

Possible sources:
- `BLE` — Bluetooth Low Energy (no network needed)
- `LOCAL TCP` — WiFi TCP port 6607 (local network only)
- `CLOUD MQTT` — Pecron's cloud servers
- `REST API` — Cloud REST API fallback

### Troubleshooting Offline Mode

**"Cannot run in offline mode: missing required fields"**

Run `--setup` first. You need internet once to fetch the `auth_key` and `tsl_cache`.

**Local TCP not connecting**

- Check that `lan_ip` is correct (device must be on same network)
- Verify port 6607 is open: `nc -zv 192.168.1.100 6607`
- Make sure your Pecron is powered on and WiFi is enabled

**BLE not connecting**

- Check that `ble_address` is correct (run `--setup` to scan)
- Make sure Bluetooth is enabled on your computer
- Pecron must be powered on and within ~30ft range

---

## Requirements

You need **three things** (plus one optional extra for Bluetooth):

### 1. Python 3.9 or newer

Check if you have it:
```bash
python3 --version
```
If you see `Python 3.9` or higher, you're good. If not:
- **Mac:** `brew install python3`
- **Raspberry Pi / Ubuntu / Debian:** `sudo apt install python3 python3-pip`
- **Windows:** Download from [python.org](https://www.python.org/downloads/)

### 2. Your Pecron account login

The **email and password** you used to create your account in the Pecron app. If you log in with Google/Apple, you'll need to set a password in the app first (go to Profile → Account Settings).

### 3. Your device key (MAC address)

This is a 12-character code that identifies your specific battery. To find it:

1. Open the **Pecron app** on your phone
2. Tap on your device
3. Tap the **⚙️ Settings** icon (top right)
4. Tap **Device Info**
5. Look for **Device Key** — it looks like `AABBCCDDEEFF`

Write this down or copy it. You'll need it during setup.

### 4. For Bluetooth (BLE) — a Bluetooth adapter

If you want to use Bluetooth monitoring (no WiFi needed):

- **Raspberry Pi 3/4/5:** Built-in ✅ — nothing to buy
- **Most laptops:** Built-in ✅ — nothing to buy
- **Desktop PCs / Le Potato / Pi Zero W:** Need a **USB BLE dongle** (~$10 on Amazon)
- **Software:** `pip3 install bleak` (the BLE library)

> BLE is optional. If you don't have a Bluetooth adapter or don't install `bleak`, the monitor works fine over WiFi and/or cloud — no errors.

---

## Step-by-Step Installation

### Step 1: Download the app

Open a terminal (Terminal on Mac, or SSH into your Pi) and run:

```bash
git clone https://github.com/attractify-logan/pecron-monitor.git
cd pecron-monitor
```

> **Don't have git?** You can also download the ZIP from GitHub and unzip it.

### Step 2: Install the dependencies

```bash
pip3 install -r requirements.txt
```

This installs three small Python packages the app needs. If you get a permissions error, try:
```bash
pip3 install --user -r requirements.txt
```

### Step 3: Run the setup wizard

```bash
python3 pecron_monitor.py --setup
```

The wizard will walk you through everything:

```
🔋 Pecron Monitor Setup

Pecron account email: you@email.com
Pecron account password: your-password

Regions:
  na — North America
  eu — Europe
  cn — China
Region [na]: na

Testing login...
✅ Login successful (uid: U12345)

--- Device Setup ---
You need your device key (MAC address). Find it in the Pecron app:
  Device → Settings (⚙️) → Device Info → Device Key
  It looks like: AABBCCDDEEFF

Device Key (or press Enter to finish): AABBCCDDEEFF
  ✅ Found: E1500LFP (AABBCCDDEEFF)
Device Key (or press Enter to finish): [press Enter]

Poll interval in seconds [60]: 60
Low battery alert threshold % [20]: 20

--- Telegram Alerts (optional) ---
Enable Telegram alerts? [y/N]: n

--- Home Assistant (optional) ---
Enable Home Assistant MQTT bridge? [y/N]: n

✅ Config saved to config.yaml
```

That's it — you're set up!

---

## Using the App

### Check your battery right now

```bash
python3 pecron_monitor.py --status
```

This connects to your battery, grabs the current data, and shows you everything:

```
==================================================
Device: AABBCCDDEEFF
==================================================
Battery:       73%
Voltage:       51.8V
Temperature:   24°C
Remaining:     8h 42m
Total Input:   145W
Total Output:  0W
AC Output:     0W @ 120V
DC Output:     0W
AC Input:      0W
DC Input:      145W
AC Switch:     OFF
DC Switch:     ON
UPS Mode:      OFF
```

### Start continuous monitoring

```bash
python3 pecron_monitor.py
```

This runs forever, checking your battery every 60 seconds (or whatever interval you set). It logs the status and sends alerts if the battery drops below your threshold. Press `Ctrl+C` to stop.

### Turn AC output on or off

```bash
python3 pecron_monitor.py --ac on
python3 pecron_monitor.py --ac off
```

### Turn DC output on or off

```bash
python3 pecron_monitor.py --dc on
python3 pecron_monitor.py --dc off
```

### See what controls your model supports

Different Pecron models have different features. Run this to see everything yours can do:

```bash
python3 pecron_monitor.py --controls
```

Example output:
```
E1500LFP (AABBCCDDEEFF):
  id=  1  RO  INT     battery_percentage  — battery power
  id= 27  RO  BOOL    ups_status_hm  — UPS status
  id= 32  RW  ENUM    ac_output_voltage_io  — AC output voltage
  id= 38  RW  BOOL    dc_switch_hm  — DC OUTPUT
  id= 40  RW  BOOL    ac_switch_hm  — AC OUTPUT
  id= 45  RW  ENUM    machine_screen_light_as  — Brightness of the screen
  ...
```

- **RW** = you can read AND write (control it)
- **RO** = read-only (sensor data, can't change it)
- **BOOL** = on/off toggle
- **ENUM** = numbered setting (like brightness levels)

### Set any control by its code name

Use `--control` followed by the code name and value:

```bash
# Turn AC on
python3 pecron_monitor.py --control ac_switch_hm on

# Turn DC off
python3 pecron_monitor.py --control dc_switch_hm off

# Set screen brightness to level 3
python3 pecron_monitor.py --control machine_screen_light_as 3
```

For BOOL controls, use `on`/`off`/`true`/`false`. For ENUM controls, use the number.

### Dump raw data (for debugging)

If something isn't working or you want to see exactly what your battery reports:

```bash
python3 pecron_monitor.py --raw
```

This prints the full JSON payload from your device. Useful if you have a newer model and want to see what data points it sends.

---

## Setting Up Alerts

Alerts notify you when your battery drops below a certain level. You configure them in `config.yaml`.

### Telegram Alerts

This is the easiest way to get push notifications on your phone.

**Step 1: Create a Telegram bot**
1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Pick a name and username for your bot
4. BotFather gives you a **bot token** — copy it

**Step 2: Get your chat ID**
1. Send any message to your new bot
2. Open this URL in your browser (replace YOUR_TOKEN with your bot token):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
3. Look for `"chat":{"id":` — that number is your **chat ID**

**Step 3: Add to config.yaml**
```yaml
alerts:
  low_battery_percent: 20
  cooldown_minutes: 30
  telegram:
    enabled: true
    bot_token: "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
    chat_id: "987654321"
```

### ntfy Alerts

[ntfy](https://ntfy.sh) is a simple push notification service. You can use the free public server or self-host.

```yaml
alerts:
  ntfy:
    enabled: true
    url: "https://ntfy.sh/my-pecron-alerts"
```

Then subscribe to `my-pecron-alerts` in the ntfy app on your phone.

### Webhook Alerts

Send alerts to any URL (Slack, Discord, custom server, etc.):

```yaml
alerts:
  webhook:
    enabled: true
    url: "https://your-webhook-url.com/endpoint"
```

The webhook receives a JSON payload with `battery_percent`, `voltage`, `remain_minutes`, `device_key`, and `message`.

---

## Automation Rules

Rules let the app automatically control your battery based on conditions. Add them to the `rules` section of `config.yaml`.

### Example: Turn off AC when battery is low

```yaml
rules:
  - name: "Low battery — turn off AC"
    condition:
      battery_below: 10
    action:
      set_ac: false
    cooldown_minutes: 30
```

### Example: Turn on AC when fully charged

```yaml
rules:
  - name: "Full charge — enable AC"
    condition:
      battery_above: 95
    action:
      set_ac: true
    cooldown_minutes: 30
```

### Example: Turn off DC when there's no solar input

```yaml
rules:
  - name: "No solar — turn off DC"
    condition:
      input_power_below: 5
    action:
      set_dc: false
    cooldown_minutes: 60
```

### Example: Shut everything off at midnight

```yaml
rules:
  - name: "Midnight shutoff"
    condition:
      schedule: "00:00"
    action:
      set_ac: false
      set_dc: false
    cooldown_minutes: 1440
```

### Available conditions

| Condition | What it checks |
|-----------|---------------|
| `battery_below: 10` | Battery percentage is at or below 10% |
| `battery_above: 95` | Battery percentage is at or above 95% |
| `input_power_below: 5` | Total input (solar/AC) is at or below 5W |
| `input_power_above: 100` | Total input is at or above 100W |
| `schedule: "08:00"` | Current time matches (24-hour format, checked every poll) |

### Available actions

| Action | What it does |
|--------|-------------|
| `set_ac: true/false` | Turn AC output on or off |
| `set_dc: true/false` | Turn DC output on or off |
| `set_ups: true/false` | Enable or disable UPS mode |

### Cooldown

`cooldown_minutes` prevents the rule from firing repeatedly. If a rule fires, it won't fire again until the cooldown expires. This prevents the app from toggling your AC on and off every 60 seconds when the battery is hovering around a threshold.

---

## Home Assistant Integration

If you use [Home Assistant](https://www.home-assistant.io/), the app can publish your Pecron as a fully auto-discovered device with sensors and switches.

### Requirements

- Home Assistant with the MQTT integration enabled
- An MQTT broker (Mosquitto is the most common — HA has an add-on for it)

### Setup

Add this to your `config.yaml`:

```yaml
homeassistant:
  enabled: true
  mqtt_host: "192.168.1.100"   # IP of your HA/MQTT broker
  mqtt_port: 1883
  mqtt_user: "mqtt_username"    # Leave empty if no auth
  mqtt_password: "mqtt_password"
```

Then start the monitor with the HA flag:

```bash
python3 pecron_monitor.py --homeassistant
```

Or just run `python3 pecron_monitor.py` — if `homeassistant.enabled` is `true` in your config, it starts automatically.

### What shows up in Home Assistant

Your Pecron appears as a device with:

**Sensors:**
- Battery percentage (%)
- Voltage (V)
- Temperature (°C)
- Input power (W)
- Output power (W)
- Remaining time (minutes)

**Switches:**
- AC output (on/off)
- DC output (on/off)
- UPS mode (on/off)

You can use these in HA automations, dashboards, and scripts just like any other smart home device.

---

## Running 24/7 on a Raspberry Pi

If you want the monitor running all the time (recommended), set it up as a system service.

### Step 1: Edit the service file

Open `pecron-monitor.service` and update the paths to match where you installed it:

```ini
[Unit]
Description=Pecron Battery Monitor
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pecron-monitor
ExecStart=/usr/bin/python3 /home/pi/pecron-monitor/pecron_monitor.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Change `User=pi` and the paths if your username or install location is different.

### Step 2: Install and start the service

```bash
sudo cp pecron-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pecron-monitor
sudo systemctl start pecron-monitor
```

### Step 3: Check it's running

```bash
sudo systemctl status pecron-monitor
```

You should see `active (running)`. To see the logs:

```bash
journalctl -u pecron-monitor -f
```

The service will:
- Start automatically on boot
- Restart if it crashes
- Reconnect if the internet drops
- Refresh authentication tokens automatically

---

## Multiple Devices

You can monitor more than one Pecron battery. Just add them during setup, or add them to `config.yaml`:

```yaml
devices:
  - product_key: "p11u2b"
    device_key: "AABBCCDDEEFF"
  - product_key: "p11usc"
    device_key: "112233445566"
```

Don't know your product key? No problem — the setup wizard finds it automatically by checking your device key against all known models.

---

## Troubleshooting

### "Login failed"
- Double-check your email and password
- Make sure you can log into the Pecron app with the same credentials
- If you use Google/Apple sign-in, you need to set a password in the app first

### "No data received — device may be offline"
- Your battery needs to be connected to WiFi
- Make sure it shows as online in the Pecron app
- The battery's WiFi module may go to sleep — open the Pecron app briefly to wake it up, then try again

### "Device not found" during setup
- Make sure you're entering the **Device Key** (MAC address), not the serial number or Device Code
- The device key is 12 characters, all uppercase letters and numbers (e.g., `AABBCCDDEEFF`)
- Check that you selected the right region (NA, EU, or CN)

### Finding your Device Key (also called "Device Code")
Depending on your Pecron app version and model, this field may be labeled **Device Key** or **Device Code** in Device Info. Either way, it's your device's MAC address — 12 hex characters like `682499E40D61` or `58DB12345678`. That's the value you need for setup.

### My model shows up as the wrong name
The API controls the product name — it's pulled from Pecron's cloud catalog, not your device. Sometimes the catalog name doesn't match the marketing name (e.g., showing "F2000LFP" for an E3600LFP). This is usually cosmetic and doesn't affect functionality. If you're getting no data despite the name mismatch, run `--diagnose` to check if the product_key/device_key binding is correct.

### Connected but no data
Run the built-in diagnostics:
```bash
python3 pecron_monitor.py --diagnose -v
```
This verifies your authentication, device binding, MQTT subscription, and waits for data — showing exactly where things break. Common fixes:
- Your device's WiFi module may be asleep — open the Pecron app on your phone briefly to wake it
- Wrong device_key (see "Device Code vs Device Key" above)
- Try `--setup` again to let the wizard auto-detect your product_key

### pip install errors
- Try `pip3 install --user -r requirements.txt`
- On Raspberry Pi, you may need: `pip3 install --break-system-packages -r requirements.txt`
- Make sure you have Python 3.9+: `python3 --version`

---

## How It Works (Technical)

The Pecron app communicates with your battery through Pecron's cloud infrastructure, which is built on the **Quectel IoT platform**. This app uses the same cloud API:

1. **Authentication** — Logs in with your Pecron email/password using an encrypted challenge-response flow (AES-CBC + SHA-256 signature)
2. **Device discovery** — Queries the product catalog to match your device key to a product model
3. **TSL fetch** — Downloads the Thing Specification Language for your model, which describes all available sensors and controls with their data types
4. **MQTT connection** — Connects to the Quectel MQTT broker over secure WebSocket (WSS on port 8443)
5. **TTLV protocol** — Sends binary TTLV (Tag-Type-Length-Value) commands to read status or write controls
6. **Data parsing** — Receives JSON responses with all sensor data (battery, voltage, power, etc.)
7. **Token refresh** — Automatically re-authenticates before the 2-hour JWT token expires

Your credentials are stored only in `config.yaml` on your local machine. The app communicates only with Pecron's own cloud servers.

---

## Security

- **Credentials** are stored locally in `config.yaml` — never uploaded anywhere
- **No telemetry** — the app doesn't phone home or track anything
- **Cloud-only communication** — data goes between your machine and Pecron's Quectel cloud servers (the same servers the official app uses)
- **Token-based auth** — your password is encrypted before transmission, and access tokens expire every 2 hours

**Tip:** Set permissions on your config file so only you can read it:
```bash
chmod 600 config.yaml
```

---

## Supported Models

The app works with any Pecron power station that connects to the Pecron app. It auto-detects your model during setup and fetches the correct data schema, so new models are supported automatically.

Currently known models:

| Model | Product Key |
|-------|------------|
| E300LFP | p11u2Q |
| C300LFP Mini | p11uXh |
| E500LFP | p11uFC |
| E600LFP | p11umP |
| E800LFP | p11uXR |
| E1000LFP | p11vxg |
| E1500LFP | p11u2b |
| E2000LFP | p11usc |
| E2200LFP | p11t8R |
| E2400LFP | p11tf9 |
| E2400LFP ADJ | p11vB4 |
| E3600 | p11tUC |
| E3600LFP | p11wV4 |
| E3800LFP | p11uJn |
| F1000LFP | p11vWw |
| F3000LFP | p11uAG |
| F5000LFP | p11vwW |
| WB12200 | p11vGo |

Don't see your model? It probably still works — run `--setup` and try it. The app checks all known product keys automatically.

---

## Use Cases

### 🚐 Van Life / RV / Off-Grid Living
Mount a Raspberry Pi Zero in your van and monitor your Pecron 24/7. Set up Telegram alerts so you get a ping on your phone when battery drops below 20% — even if you're inside a store or away from the van. Add automation rules to shut off AC overnight so you don't wake up to a dead battery. Track solar input throughout the day to see how your panels are performing.

### 🏠 Home Backup Power
Use your Pecron as a UPS for your home network, fridge, or medical equipment. The app can keep UPS mode enabled and alert you immediately if power drops below a critical level. Set up rules to shed non-essential loads automatically — turn off DC when battery hits 30%, then AC at 15% — so your most important devices stay powered longest.

### 🏕️ Remote Cabin / Off-Grid Site
Monitor a battery that's miles away. As long as it has WiFi (even a cellular hotspot), you can check status and control outputs from anywhere in the world. Perfect for remote cabins, construction sites, or weather stations where you can't physically check the battery every day.

### ☀️ Solar Performance Tracking
Run the monitor continuously and watch your input power throughout the day. See exactly when your panels start producing, peak output, and when they taper off. Set up alerts for when input drops to zero (cloudy day? panel issue? shade?) so you can investigate. Over time, you'll learn your system's patterns.

### 📊 Home Assistant Dashboard
Add your Pecron to your existing smart home setup. See battery level on your HA dashboard alongside your other devices. Create HA automations that interact with your Pecron — turn on the AC output when you arrive home, or flash your smart lights when battery is critically low.

### 🎪 Events / Job Sites / Film Sets
Running power for an event, market booth, or film shoot? Monitor the battery from your laptop so you're not constantly walking over to check it. Get an alert with enough time to swap batteries or start a generator before everything goes dark.

### 🔧 Fleet Management
If you manage multiple Pecron units (rental company, disaster relief, construction), monitor all of them from one place. Each device shows up independently with its own alerts and automation rules.

---

## Frequently Asked Questions

### How do I monitor my Pecron battery without the phone app?

Install this tool on any computer (Raspberry Pi, Linux server, Mac, Windows). It connects directly to Pecron's cloud servers using your account credentials — no phone needed. Run `python3 pecron_monitor.py --status` for a one-shot check, or run it continuously for 24/7 monitoring with alerts.

### Does this work with my Pecron model?

If your power station works with the official Pecron app, it almost certainly works with this tool. The app auto-detects your model during setup. It's been tested with the E1500LFP and supports all models on the Quectel IoT platform including E300LFP, E500LFP, E600LFP, E800LFP, E1000LFP, E1500LFP, E2000LFP, E2200LFP, E2400LFP, E3600, E3600LFP, E3800LFP, F1000LFP, F3000LFP, F5000LFP, and more.

### Can I control my Pecron from a Raspberry Pi?

Yes. You can turn AC and DC outputs on/off, toggle UPS mode, change screen brightness, and more — all from the command line. Run `python3 pecron_monitor.py --controls` to see every control your model supports.

### Can I integrate my Pecron with Home Assistant?

Yes. The app includes a built-in Home Assistant MQTT bridge that publishes auto-discovery configs. Your Pecron shows up as a device in Home Assistant with battery sensors and AC/DC/UPS switches. See the Home Assistant section above for setup instructions.

### Can I get alerts when my Pecron battery is low?

Yes. The app supports Telegram, ntfy, and generic webhook alerts. Set your threshold (e.g., 20%) in `config.yaml` and you'll get a push notification when the battery drops below it.

### Can I automate my Pecron based on battery level?

Yes. Add rules to `config.yaml` to automatically turn AC/DC on or off based on battery percentage, solar input power, or time of day. For example, you can turn off AC when battery drops below 10% or shut everything off at midnight.

### Does my phone need to stay connected?

No. This tool talks directly to Pecron's cloud API over the internet. Your battery just needs to be connected to WiFi (which it already is if you've set it up in the Pecron app at least once). Your phone can be off, in another country, whatever.

### Is this an official Pecron product?

No. This is an independent open-source project that reverse-engineered the Pecron cloud API. It is not affiliated with, endorsed by, or supported by Pecron. Use at your own risk.

### How do I get my device key?

Open the Pecron app → tap your device → tap the ⚙️ Settings icon → tap Device Info → look for "Device Key". It's a 12-character code like `AABBCCDDEEFF` (this is your device's MAC address).

### Can I monitor multiple Pecron batteries?

Yes. Add multiple devices during setup or add them to the `devices` list in `config.yaml`. Each device is monitored independently.

### Does this work with solar panels?

Yes. The app shows your total input power (solar + AC charging), DC input power (solar specifically), and remaining charge time. You can also set automation rules based on input power — for example, turn off DC output when solar input drops below 5W.

### What data can I see?

Battery percentage, voltage, temperature, total input/output power, AC output power and voltage, DC output power, AC input power, DC/solar input power, remaining time, AC/DC switch states, UPS mode, and expansion battery pack data (if connected).

### Is my password safe?

Your password is stored only in `config.yaml` on your local machine. When authenticating with Pecron's servers, the password is AES-encrypted before transmission (the same encryption the official app uses). The app never sends your credentials anywhere except Pecron's own cloud servers.

---

## License

MIT — do whatever you want with it.
