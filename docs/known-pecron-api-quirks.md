# Known Pecron API Quirks

A running log of bugs, inconsistencies, and undocumented behavior observed in Pecron's cloud API and device firmware. Each entry names the first project to document the behavior so credit stays with the original discoverer. If you've hit something new, PRs welcome. Please cite the bug report that surfaced it.

## `remain_time` and `remain_charging_time` report identical values

**First documented by:** [jsight/unofficial-pecron-api issue #1](https://github.com/jsight/unofficial-pecron-api/issues/1)
**Confirmed on:** E300LFP, E1500LFP, E3800LFP
**Affects:** REST API, cloud MQTT

The Pecron cloud API returns the same integer for both `remain_time` ("discharging time") and `remain_charging_time` ("full charging time"), regardless of whether the device is charging or discharging. Only one of the two values is meaningful at any given moment; the monitor infers which by looking at whether net power is flowing in or out.

This is a firmware/API bug, not something any client library can fix. Originally reverse-engineered and published by jsight; we're citing their evidence directly.

## `high_frequency_reporting` is ignored by E3600LFP firmware

**First documented by:** [@brucehoult in pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Affects:** Cloud MQTT on E3600LFP (E3800LFP still honors the setting)

TSL property `high_frequency_reporting` (id=100, ENUM) is meant to make the device push telemetry every few seconds instead of the normal cadence. It works on E3800LFP, E1500LFP, and most other Pecron models, but on the E3600LFP the setting has no observable effect. @brucehoult verified this across multiple test cadences: sending `=3` every 20 seconds, every 5 minutes, or never at all all produce identical behavior, one telemetry packet per value-type every ~20 minutes.

pecron-monitor skips the send entirely for E3600/E3600LFP (see `MODEL_BEHAVIOR` in `constants.py`) to avoid wasting cloud requests. The only observed way to force faster telemetry on an E3600 is to leave the official Pecron mobile app open on the device's status screen, but that causes the "Insufficient resources" quota exhaustion documented in the next entry, so it's not a real workaround.

## Pecron cloud `code 4026 Insufficient resources` is a per-account polling rate-limit

**First documented by:** [@brucehoult in pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Root cause isolated by:** [@brucehoult in pecron-monitor issue #29 (2026-05-01..03)](https://github.com/attractify-logan/pecron-monitor/issues/29)
**Affects:** Cloud MQTT and REST, any model

Pecron's cloud returns `type=BUSI-ERROR, code=4026, msg='Insufficient resources in the manufacturer's account. Please contact the device manufacturer.'` after the account hits a daily polling quota. The cap is roughly **1280 polls/day** per account. The window resets at 00:00 UTC.

The original framing — a Pecron-side global quota that nothing on our end could affect — was wrong. @brucehoult disproved it by varying `poll_interval`:

| `poll_interval` | Outcome |
| --- | --- |
| 60s | 4026 fires daily around 23:00 UTC |
| 62s | 4026 fires at 23:45 UTC |
| 63s | clean through midnight UTC |
| 120s | clean indefinitely |

The poll loop sleeps `poll_interval` seconds *then* does work, so a configured 60 produces a ~65s effective cycle (~1329 polls/day) — sitting on top of the 1280 cap. 63s gives ~1271/day, just under it.

`pecron-monitor` defends against this in three ways:

1. **Default `poll_interval` is 70s** (margin over the empirical 63s floor).
2. **Hard floor of 63s** at startup. Lower values raise `ValueError` with a pointer to this section. 63 is @brucehoult's empirical floor: at `poll_interval=60` his account exhausts the daily budget at ~23:00 UTC; at 63 the same budget stretches to 23 × 63/60 = 24.15h, crossing the 00:00 UTC reset. See `MIN_POLL_INTERVAL` / `RECOMMENDED_POLL_INTERVAL` in `monitor.py`.
3. **One-shot ERROR on 4026 receipt** that explains the rate-limit cause and recommends raising `poll_interval`, instead of the generic warning the monitor used to emit.

If you need automations to keep working through 23:00 UTC, raise `poll_interval` to 70 (the default) or higher. The cap appears to be account-level: high-frequency reporting (`high_frequency_reporting=3`) and an open Pecron app both burn the same budget faster.

Regional caveat: confirmed on @brucehoult's EU-region E3600LFP. At least one NA-region account running `poll_interval=60` against three devices has not produced 4026 in 30+ days of journals — so the cap may differ by region or account tier. Treat the 1280/day figure as a useful upper bound, not a contract.

## E3600LFP battery capacity is 3072Wh, not 3600Wh

**First documented by:** [@brucehoult in pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Affects:** Any calculation using `BATTERY_CAPACITY_WH`

The "3600" in E3600LFP is the inverter wattage, not the battery capacity. The actual LiFePO4 pack is 3072Wh, identical to the F3000LFP. Easy to get wrong because every other model in the lineup names itself after the pack size (E1500LFP = 1536Wh, E3800LFP = 3840Wh, etc.). v0.7.0 shipped the wrong value; v0.7.2 corrects it.

## E3600LFP / E3800LFP telemetry arrives in alternating MQTT packets

**First documented here:** [pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Affects:** Cloud MQTT on E3600LFP and E3800LFP

These models split telemetry across alternating packet shapes: battery/status,
voltage/power, and settings can arrive separately, so one message is not a
complete snapshot. The cadence differs by firmware:

- E3800LFP responds to `high_frequency_reporting=3`; packet shapes then arrive
  roughly 10-15 seconds apart.
- E3600LFP ignores that property. Hardware testing found each packet type stayed
  on an approximately 20-minute cadence, with packet types offset from each
  other. The official app's open status screen can trigger faster reports, but
  no app-free cloud command is known ([issue #30](https://github.com/attractify-logan/pecron-monitor/issues/30)).

pecron-monitor merges partial packets into its last-known-good state. During
startup it also enables high-frequency reporting briefly on models where the
setting is effective, then disables it again. E3600/E3600LFP are explicitly
excluded from those writes because they do not change the device's cadence.

## Persistent `high_frequency_reporting=3` burns cloud quota (error 4026)

**First documented here:** [pecron-monitor v0.7.0 changelog](../CHANGELOG.md)
**Affects:** Cloud MQTT on models where high-frequency reporting is effective

Leaving high-frequency reporting enabled indefinitely can return error code 4026
(`"Insufficient resources in manufacturer's account"`) and stop cloud telemetry.
The monitor uses it only for the initial cache warm-up, then restores the normal
reporting mode. Setting `high_freq_warmup_seconds: 0` disables that warm-up.

## `code 4007 "device is not bound"` is frequently a false positive

**First documented here:** `monitor.py:549`
**Affects:** REST API and cloud MQTT control traffic

When sending a control command or verifying a device, Pecron's cloud sometimes replies with code 4007 even for devices that are bound correctly and actively streaming telemetry. It appears to be either a transient or a stale/cached cloud-side state.

Treat 4007 as actionable only if it persists *and* the device also never produces telemetry. The monitor logs it as a warning once per session to avoid alert fatigue.

## E3600LFP local TCP telemetry is inconsistent and not yet verified in normal operation

**First documented here:** [pecron-monitor issue #14](https://github.com/attractify-logan/pecron-monitor/issues/14)
**Tracked in:** [pecron-monitor issue #30](https://github.com/attractify-logan/pecron-monitor/issues/30)
**Affects:** Local TCP (port 6607) on E3600LFP

Early captures returned only eight settings fields and no battery, voltage,
power, or temperature. A later offline-only low-battery/shutdown capture did
return battery percentage, voltage, and temperature over local TCP, but its
power and remaining-time values were zero/default. That proves richer local data
is possible in at least one device state, not that it is reliable during normal
operation.

Current releases use a longer inter-packet timeout and active retries for the
E3600/E3800 packet family. This behavior is hardware-confirmed on E3800LFP; an
E3600LFP `--local --status -v` / `--raw --local -v` capture with every official
app instance closed is still needed to verify normal-operation cadence and field
completeness. Until then, local mode is a testable lead rather than a documented
workaround for the E3600 cloud limit.

## Pecron device MAC address matches the `device_key` byte-for-byte

**First documented here:** incidentally, in this repo's setup-wizard output
**Affects:** All models seen so far

The 12-hex-char `deviceKey` returned by the cloud device-list API is the same as the Wi-Fi MAC address burned into the device's radio. `device_key=682499E40D61` appears on the LAN as MAC `68:24:99:e4:0d:61`. This is useful for LAN auto-discovery (see `lan_scan.py`): a single subnet ARP scan is enough to locate every bound device, no active handshake needed.

## F5000LFP local TCP returns no fields, and local-TCP control writes silently fail

**First documented by:** [noahbalboah/pecron-homeassistant](https://github.com/noahbalboah/pecron-homeassistant/blob/main/docs/pecron-api-quirks.md)
**Confirmed on:** F5000LFP
**Affects:** Local TCP (port 6607)

Extends the E3600LFP local-TCP entry above. On the F5000LFP a local-TCP read returns *no* data fields at all — not even the settings subset the E3600LFP returns. The connection handshakes and the encrypted channel comes up, then the read body is empty (`No data fields in local read response`, even after retry).

More consequentially, **local-TCP control *writes* silently fail**: a `--control` write over local TCP is accepted with no error, but the value never actually changes (verified by reading it back over cloud REST minutes later — it stays at the old value). The reliable path for both reading settings and writing them is the **cloud REST API** (`getDeviceBusinessAttributes` / `batchControlDevice`, i.e. `--rest-only`). Setting `high_frequency_reporting` to a LAN mode does not make local reads return data, and on F5000LFP that write did not even persist (read back as the prior value). Practical rule for F5000LFP: use `--rest-only` for any settings write, and verify with a REST read-back.

## F5000LFP setting writes (e.g. AC charge limit) apply with minutes of propagation lag

**First documented by:** [noahbalboah/pecron-homeassistant](https://github.com/noahbalboah/pecron-homeassistant/blob/main/docs/pecron-api-quirks.md)
**Confirmed on:** F5000LFP
**Affects:** Cloud REST (`batchControlDevice`)

A cloud-REST write of `ac_charge_stop_value_iaos` (AC charge limit) returns `code 200` immediately, but the new value is not reflected by `getDeviceBusinessAttributes` for several minutes — an immediate read-back still shows the old value, while a read ~30 minutes later shows the new one. Don't conclude a write failed from a read taken seconds later; re-read after a few minutes before retrying.

On F5000LFP the AC charge *limit* (`ac_charge_stop_value_iaos`) — not the charge *speed* (`ac_charging_power_ios`), which often doesn't bind — is the lever that governs grid charging; solar/DC charges independently above the limit, which makes a "grid tops up to X%, solar fills the rest" strategy possible.
