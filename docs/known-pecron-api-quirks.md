# Known Pecron API Quirks

A running log of bugs, inconsistencies, and undocumented behavior observed in Pecron's cloud API and device firmware. Each entry names the first project to document the behavior so credit stays with the original discoverer. If you've hit something new, PRs welcome — please cite the bug report that surfaced it.

## `remain_time` and `remain_charging_time` report identical values

**First documented by:** [jsight/unofficial-pecron-api issue #1](https://github.com/jsight/unofficial-pecron-api/issues/1)
**Confirmed on:** E300LFP, E1500LFP, E3800LFP
**Affects:** REST API, cloud MQTT

The Pecron cloud API returns the same integer for both `remain_time` ("discharging time") and `remain_charging_time` ("full charging time"), regardless of whether the device is charging or discharging. Only one of the two values is meaningful at any given moment — the monitor infers which by looking at whether net power is flowing in or out.

This is a firmware/API bug, not something any client library can fix. Originally reverse-engineered and published by jsight; we're citing their evidence directly.

## E3600LFP / E3800LFP telemetry arrives in alternating MQTT packets

**First documented here:** [pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Affects:** Cloud MQTT on E3600LFP and E3800LFP

On these two models the cloud sends telemetry in 2-3 alternating packet shapes spaced roughly 10-15 seconds apart. A single MQTT message will contain only battery+status, or only voltage+power, or only settings — never everything in one payload. Clients that request data once and wait a fixed interval will see a partial picture.

pecron-monitor works around this by:

1. Enabling `high_frequency_reporting=3` at startup for a short warm-up window (see `high_freq_warmup_seconds` in `config.yaml`, default 60s) so the full packet sequence arrives quickly.
2. Disabling it again after warm-up — see the next quirk for why.
3. Merging partial packets with a last-known-good cache (`_state_cache` in `ha_bridge.py`) so Home Assistant entities don't flap to `unknown` between packets.

## Persistent `high_frequency_reporting=3` burns cloud quota (error 4026)

**First documented here:** [pecron-monitor v0.7.0 changelog](../CHANGELOG.md)
**Affects:** Cloud MQTT, any model

Leaving `high_frequency_reporting` enabled indefinitely eventually returns error code 4026 (`"Insufficient resources in manufacturer's account"`) and stops all cloud telemetry until it's disabled. The monitor enables it only long enough to fill the initial telemetry cache, then flips it back to 0.

If you set `high_freq_warmup_seconds: 0`, be aware that slow devices (E3600/E3800 per above) may never produce a complete status log, and setting it to a very large number risks tripping 4026.

## `code 4007 — "device is not bound"` is frequently a false positive

**First documented here:** `monitor.py:549`
**Affects:** REST API and cloud MQTT control traffic

When sending a control command or verifying a device, Pecron's cloud sometimes replies with code 4007 even for devices that are bound correctly and actively streaming telemetry. It appears to be either a transient or a stale/cached cloud-side state.

Treat 4007 as actionable only if it persists *and* the device also never produces telemetry. The monitor logs it as a warning once per session to avoid alert fatigue.

## E3600LFP local TCP read returns only settings fields (no telemetry)

**First documented here:** [pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Affects:** Local TCP (port 6607) on E3600LFP

A standard TTLV read command to the E3600LFP over local TCP returns exactly 8 fields: `ac_output_voltage_io`, `ac_output_frequency_io`, `noastime_io`, `ac_switch_hm`, `auto_light_flag_as`, `machine_screen_light_as`, `device_manual`, `high_frequency_reporting`. Battery, voltage, power, and temperature are missing from this response.

Unlike the E1500LFP (which returns the full property set in a single local read), the E3600LFP appears to restrict local-TCP responses to control-type properties. Workarounds attempted in `fix/e3600-data`: parsing the `extData` field on cloud MQTT bus messages, falling back to the REST `getDeviceBusinessAttributes` endpoint, and registering new TSL IDs for E3600-specific property shifts (e.g. `ac_switch_hm` at id=56 instead of 40). Investigation ongoing.

## Pecron device MAC address matches the `device_key` byte-for-byte

**First documented here:** incidentally, in this repo's setup-wizard output
**Affects:** All models seen so far

The 12-hex-char `deviceKey` returned by the cloud device-list API is the same as the Wi-Fi MAC address burned into the device's radio. `device_key=682499E40D61` appears on the LAN as MAC `68:24:99:e4:0d:61`. This is useful for LAN auto-discovery (see `lan_scan.py`) — a single subnet ARP scan is enough to locate every bound device, no active handshake needed.
