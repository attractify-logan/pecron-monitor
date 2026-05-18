# Changelog

All notable changes to pecron-monitor are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project uses [Semantic Versioning](https://semver.org/).


## [0.7.14] - 2026-05-17

### Fixed
- **Home Assistant sensor discovery no longer emits invalid `entity_category: config` payloads**. Live HA testing showed config-category sensors are rejected by Home Assistant. Sensor entities that were previously classified as configuration are now published as diagnostic instead, keeping them out of the main view while preserving valid discovery payloads.

## [0.7.13] - 2026-05-17

### Fixed
- **Home Assistant discovery payload changes apply on service restart**. The bridge now clears each current retained discovery topic immediately before republishing it, forcing HA to reload changed discovery fields without a manual MQTT integration reload. Set `homeassistant.clear_discovery_on_startup: false` to keep the previous publish-only behavior.

## [0.7.12] - 2026-05-17

### Added
- **Rules engine state and init/external-command actions** (#56). Rules can now gate on persisted `state` / `states`, fire once at service startup via `init`, transition state with `set_state`, and run external commands with rule/device telemetry JSON on stdin via `run_command`. State is persisted to `~/.pecron-monitor-rules.json` by default, configurable with `rule_state.path` or `PECRON_RULE_STATE_PATH`.

## [0.7.11] - 2026-05-17

### Added
- **Voltage-based automation and shutdown-restore triggers** (#57, partial #56). Rules now support `voltage_below` and `voltage_above` conditions, and `restore_outputs_after_shutdown` can use optional `shutdown_threshold_voltage` in addition to the existing SoC threshold. This lets LFP users trigger low-battery behavior from pack voltage when SoC has drifted out of calibration.

## [0.7.10] - 2026-05-17

### Added
- **Docker support** (#71). Added a `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and README instructions for building and running a local container image with `config.yaml` mounted read-only at `/config/config.yaml`.
- **Console script entrypoint** (#68). Editable/package installs now expose `pecron-monitor`, while source-checkout usage via `python3 pecron_monitor.py` remains available.
- **Ruff lint/format checks in CI** (#67). CI now runs `ruff check .` and `ruff format --check .` before tests.

### Changed
- **Local/offline mode can use fast polling again** (#70). The 63s cloud quota floor still applies whenever cloud polling can be used, but `--local`/offline mode may use lower `poll_interval` values for LAN/BLE monitoring.
- **Package metadata is back in sync** (#64). `pyproject.toml`, `pecron_monitor.__version__`, README, and changelog now all report the same release version, and CI checks Python/package/changelog consistency.

### Fixed
- **NA cloud login domain fallback** (#31). North America login now retries once with the alternate Pecron/Quectel user domain when the primary domain returns a domain-related API error.
- **MQTT CONNACK recovery** (#69). Broker failures such as `Client identifier not valid` or `Server unavailable` now trigger an in-process MQTT client rebuild after a cooldown, matching the manual service-restart recovery path.
- **Setup credential handling** (#65). The setup wizard now hides password prompts, warns when an existing `config.yaml` is group/world-readable, and writes new config files with `0600` permissions on POSIX systems.

## [0.7.9] - 2026-05-03

### Added
- **Restore AC/DC switch state after a low-battery shutdown** (#59). Opt-in via the new `restore_outputs_after_shutdown` config block (default `enabled: false`). When a device transitions offline at SoC at or below `shutdown_threshold_pct` (default 10%), the monitor snapshots the current AC/DC switch state to `~/.pecron-monitor-state.json`. When the device later comes back online after a gap of at least `minimum_offline_seconds` (default 120s — filters out cloud blips), a background worker re-issues the saved AC/DC commands every `retry_interval_seconds` (default 30s) until observed state matches the snapshot or `retry_timeout_seconds` (default 600s) elapses. Snapshots older than `snapshot_max_age_seconds` (default 24h) are discarded. Local-TCP path is preferred (matches the latency Bruce measured in #57). State verification is via observed `latest_data` rather than `send_control` return values, which makes the loop robust against the LCD-at-0%-silently-rejects-commands pattern Bruce documented. Reproducer scenario from #57 / @brucehoult.

## [0.7.8] - 2026-05-03

### Fixed
- **`device_status_hm` freezes at "Shut Down" on standalone PPS while the device is actively running** (#45). Sibling pattern to PR #44 (the `soc_percent` fix). `device_status_hm` is carried only by overall-shape packets, so on a standalone E1500LFP that primarily emits host-shape packets during active operation the cached status freezes at the last overall reading — typically "Shut Down" from before the device woke up. HA misreports the device as Shut Down while it's actively charging or discharging. Unlike `soc_percent`, there's no host-shape equivalent to mirror, so the fix infers status from observed power activity instead: when cached `device_status_hm == "Shut Down"` and any of `total_input_power` / `total_output_power` / `ac_output_power` / `dc_output_power` are non-zero, the cache is overridden with the inferred status (`Charging` if input > 0, else `AC Discharge` / `DC Discharge` based on which output dominates). The next genuine overall-shape packet still wins; the inference only fires when cache says "Shut Down" but power contradicts it. Reproducer reported on a live E1500LFP at 74% battery discharging at 233W AC.

## [0.7.7] - 2026-05-03

### Fixed
- **Suppress LOCAL TCP shutdown-window zero-frames** (#60). When the inverter is gating off during a low-battery shutdown, local TCP returns a frame carrying fresh `battery_pct=0` and voltage but zeroed `total_input_power`, `total_output_power`, and `remain_time`. Those zeroes are technically real-time-truth (no current is flowing because the inverter is off) but they overwrote the cloud's last-known-good values in the misleading status log line and triggered an unnecessary HA publish + alert/rule pass during the 1-2 minute shutdown transition. `_process_data` now detects the shape (source in `{"LOCAL TCP", "BLE"}` + `battery_pct=0` + all power=0 + `remain<=0`) and skips the status log, HA publish, and rule evaluation for that single frame. Cloud MQTT continues to drive HA. Reproduction reported by @brucehoult in #57 / #60.

## [0.7.6] - 2026-05-03

### Changed
- **`poll_interval` default raised from 60 to 70** (#29). Pecron's cloud rate-limits per-account at roughly 1280 polls/day; configured 60 produced a ~65s effective cycle (~1329 polls/day) which sat right on top of the cap and tripped `code 4026 'Insufficient resources'` daily around 23:00 UTC. 70s gives ~1152 polls/day with comfortable margin. The setup wizard now suggests 70 in interactive mode and writes 70 in `--auto` mode.
- **BREAKING: `poll_interval` hard floor of 63s** (#29). Configs with `poll_interval` below 63 (including the previous default of 60) now raise `ValueError` at startup with a pointer to the rate-limit math. 63 is @brucehoult's empirically-derived floor: at 60 his account exhausts the daily budget at ~23:00 UTC, at 63 the same budget stretches to 23×63/60 = 24.15h and crosses the 00:00 UTC reset. Upgraders with `poll_interval: 60` in config must bump to 63+ (or remove the field to inherit the new 70 default).

### Added
- **Startup warning for `poll_interval` between 63 and 69**. References issue #29 so operators can decide whether to raise toward the recommended 70 or accept the slim margin.
- **One-shot ERROR log on receipt of `code 4026`**. The monitor previously logged a generic `WARNING Cloud system message: code=4026 ...` that buried the cause. Now also emits an ERROR explaining the per-account cap and recommending a `poll_interval` bump. Subsequent 4026s in the same session stay at WARNING to avoid log spam.

### Documentation
- **`docs/known-pecron-api-quirks.md` 4026 section rewritten**. The original entry framed 4026 as an unfixable Pecron-side global quota; @brucehoult's evidence in #29 (poll_interval matrix from 60s through 120s) showed it's actually a per-account polling rate-limit. Replaced with the corrected understanding plus a regional caveat (NA accounts may not hit it as easily).
- README config example updated; new note explaining the floor and the rationale.

Credit: root cause isolated by @brucehoult across #14 and #29.

## [0.7.5] - 2026-04-19

### Fixed
- **Ghost per-pack expansion sensors on standalone PPS**. Devices with no expansion packs attached (e.g. standalone E1500LFP, E3800LFP) published `pack_0..3_battery/voltage/current/temp/status` to HA with values of 0, creating 20 ghost sensor entities per device page that could never show real data. `publish_state` now detects `charging_pack_status == 4` ("No Connection") per slot and omits that slot's fields from the cached state JSON, so HA shows Unknown for the disconnected slots and next state publish strips any stale retained values.
- **Ghost per-port DC-input sensors on devices without solar or with idle ports**. The DC5521 barrel jack, GX16-MF1, and GX16-MF2 inputs each expose three sensors (voltage, current, power). Ports reporting 0 across all three are suppressed in the cache, same Unknown-instead-of-0 effect.
- **Battery (SOC) showing Unknown on standalone PPS that only emit host-shape packets** (notably E1500LFP). `soc_percent` now falls back to `host_percent` whenever it isn't independently populated. Devices with expansion packs still get their distinct overall-SOC value from whichever packet shape carries it; the fallback only kicks in when no overall-SOC ever arrives.

## [0.7.4] - 2026-04-19

### Fixed
- **HA voltage sensor no longer dips to 0.0V on packet flaps** (#36). The cloud MQTT / local TCP transport occasionally delivers a settings-only packet shape that resolves the voltage field to 0.0V even while the device is healthy. ha_bridge previously accepted the 0.0V as a real reading, so HA graphs showed spurious dips and low-voltage alerts could false-fire. Voltage is now treated as sticky-non-zero: a 0.0V value is ignored whenever the cache holds a positive reading, and the initial cache is left empty (HA shows Unknown) instead of being seeded at 0 before real data arrives. Credit for the reproduction: @brucehoult in #14.

## [0.7.3] - 2026-04-19

### Changed
- **Home Assistant device view now groups entities into Configuration / Diagnostic sections** (#34). Non-essential entities (UPS mode, screen brightness, per-pack voltages, battery temps, AC output voltage/frequency, etc.) get HA's standard `entity_category` hint in their MQTT discovery payload, so HA collapses them under labeled sections instead of flooding the main device view with 40+ entries. Everyday readings (battery %, voltage, temperature, total power in/out, AC/DC switches, remaining time) stay in the main view as before. No config changes needed. Pull, restart, HA reorganizes automatically.

## [0.7.2] - 2026-04-19

Targeted E3600LFP follow-ups from #14, informed by detailed testing and debugging from @brucehoult and @derekclawson.

### Fixed
- **E3600 / E3600LFP battery capacity was wrong** (#14). v0.7.0 set `BATTERY_CAPACITY_WH["E3600LFP"] = 3600`, but "3600" in the model name refers to the inverter wattage, not the pack. Actual LiFePO4 pack is **3072Wh** (same as the F3000LFP). Caught by @brucehoult. All displays, alerts, and time-to-empty estimates now use the correct capacity.
- **`--rest-only` stopped refreshing data after the first poll** (#14). In rest-only mode, `_request_status()` guarded the REST fetch with `if dk not in self.latest_data:`, so after cycle 1 the device appeared frozen. Now re-fetches every poll when `rest_only=True`. Fix from @brucehoult.
- **`--status` and `--raw` left the device in high-freq mode on exit**. Both one-shot commands now call `_disable_high_freq_reporting()` before shutting down MQTT, preventing a single CLI invocation from leaving the device burning cloud quota indefinitely.

### Changed
- **Skip `high_frequency_reporting` sends on models where it's a known no-op** (#14). `@brucehoult` verified on E3600LFP that the setting has no observable effect across multiple cadences; telemetry arrives every ~20 minutes regardless. New `MODEL_BEHAVIOR` map in `constants.py` lets us mark a model's `high_freq_effective=False`; the enable/disable helpers and `--status` path respect it. E3600 and E3600LFP are now skipped. E3800LFP and E1500LFP behavior is unchanged.

### Docs
- `docs/known-pecron-api-quirks.md` updated with three new entries:
  - E3600LFP ignores `high_frequency_reporting` (credit: @brucehoult).
  - Pecron cloud returns `code 4026 Insufficient resources` daily at ~23:00 UTC until 00:00 UTC reset (credit: @brucehoult, reproducible with the service stopped and the app closed).
  - E3600LFP battery capacity is 3072Wh, not 3600Wh.

## [0.7.1] - 2026-04-19

### Fixed
- **Cloud login failure no longer strands the monitor in offline mode forever** (#23). When a token refresh hits a transient network error (DNS outage, router reboot, ISP blip, etc.), the monitor falls back to offline mode as before, then retries cloud re-login every `cloud_retry_interval` seconds (default: 300s). On a successful retry, MQTT is reconnected and local transports are refreshed automatically. User-requested `--offline` runs are never retried.
- **Home Assistant MQTT bridge now retries a failed initial connection** (#23 follow-up). If the local MQTT broker is down when the monitor starts, the bridge attempts to reconnect every `homeassistant.retry_interval` seconds (default: 60s) instead of giving up permanently. `paho-mqtt`'s built-in auto-reconnect already handles drops after an established connection; this fix closes the startup gap.

### Added
- Two config knobs (both optional, safe defaults):
  - `cloud_retry_interval` at the top level: seconds between cloud recovery attempts.
  - `homeassistant.retry_interval`: seconds between HA broker reconnect attempts.

## [0.7.0] — 2026-04-04

### Added
- **Smart high-frequency warm-up mode** — new `high_freq_warmup_seconds` config option (default: 60s). High-freq reporting is enabled briefly at startup to quickly populate initial telemetry, then disabled automatically to preserve cloud quota (prevents error 4026: “Insufficient resources in manufacturer's account”).
- **E3600 / E3600LFP battery capacity mapping** — added both models to `BATTERY_CAPACITY_WH` (3600Wh).

### Fixed
- **Misleading “0.0V” status logs** — when the MQTT packet with battery% arrives before the voltage packet (common on E3600/E3800 alternating packet shapes), the monitor now skips the status log until voltage is available. HA state + alerts still update with partial data.

### Changed
- **Stale/duplicate status log spam** — continuous mode now tracks last logged values per device and only emits a status log when key values actually change (battery%, voltage, temp, in/out power). HA updates + alert/rule evaluation still run every poll.

## [0.6.5] — 2026-03-25

### Fixed
- **`--status` and `--raw` modes now enable high-frequency reporting** before requesting data, matching continuous monitor behavior. This should fix E3600LFP and similar devices that only send telemetry when high-freq mode is active. (#14)

### Improved
- **MQTT debug logging** now shows kv field names for each received packet
- **`--status` automatically retries** if devices have incomplete telemetry after initial wait (up to 30s total for slow-reporting devices)

## [0.6.4] — 2026-03-25

### Added / Changed
- **E1000 support** tested against an E1000
- **Transport selection** allow user to explicitly specify whether to use local, nolocal, rest-only or all transports for the given request (where appropriate), mostly for testing/debugging
- **Set control via REST** added ability to set a control value via REST interface.
- **Compute real charge/discharge times** based on the battery capacity and real-time current, compute the estimated time to charge/discharge.

### Improved
- Use device-cached `controls` mapping for local TTLV id→property conversion rather than fixed mapping (which was wrong for E1000).
- Added detailed debug logging for local TTLV → kv mapping (per-field id→code, nested fields, array elements).
- Extended info in Status output, including computed actual power flow to battery.

### Fixed
- **REST fallback** — Fixed the parsing of the REST response payload

### Notes
- Local setting of numeric value controls (like display brightness) isn't working for me on the E1000, not sure if it works on other models. Setting BOOL controls (line AC output) works OK.
- The internal calculation of remaining charge/discharge times is broken in the E1000 at least, not sure about other models.
  It seems to be using the delta between "input" and "output" power to compute the times, but the AC "input" doesn't include passthrough power, but the AC "output" does, massively skewing the result.

## [0.6.3] — 2026-03-21

### Fixed
- **E3600LFP / E3800LFP telemetry** — These models return only settings (switches, screen brightness) over local TCP, not battery/voltage/power data. Previously this blocked cloud MQTT telemetry from being processed, resulting in "0%" or no data. The monitor now detects settings-only local TCP responses and correctly falls back to cloud MQTT for telemetry. (#14)
- **MQTT alternating packet accumulation** — Devices that send telemetry in alternating incomplete MQTT packets (battery % in one, power data in another) now accumulate properly before being displayed. Data is processed from the merged accumulator instead of individual packets.

### Improved
- **Connection behavior table** in README documenting per-model local TCP vs cloud MQTT capabilities
- Clear debug logging: `Local TCP data is settings-only for [device] (telemetry from cloud)` instead of cryptic "Skipping invalid/empty data" messages
- Cleaned up stale branches (fix/e3600-data, fix/state-caching-and-diagnostics)

## [0.6.2] — 2026-03-15

### Improved
- **Model-specific HA entities** — WB12200 users no longer see 38 irrelevant "Unknown" entities (solar ports, AC/DC output, packs, etc.). Portable power stations (E-series, F-series) no longer see WB12200-specific battery management entities (charging/discharge limits, heating mode, beep). Entity counts: PPS = 65, WB12200 = 27.
- **Device key in HA device name** — shows as "Pecron E3800LFP (ABC123)" for multi-device setups

## [0.6.1] — 2026-03-15

### Added
- **Per-port solar/DC input sensors** — DC5521 (barrel jack), Solar Port 1 (GX16-MF1), Solar Port 2 (GX16-MF2) each expose voltage, current, and power as separate HA entities
- **AC output frequency & power factor** — actual readings (not settings)
- **Per-pack expansion battery sensors** — Packs 0-3 each expose battery %, voltage, current, temperature, and status (No Charge / Cascade Charging / Balance No Charge / Balanced Charging / No Connection)

### Fixed
- **Offline mode no longer spams high-freq errors** — `high_frequency_reporting` commands only sent when cloud MQTT is connected. SeanUhTron's local-only F3000LFP setup no longer logs `Cannot send control` errors every 20s.
- Downgraded "Cannot send control" from ERROR to DEBUG level

## [0.6.0] — 2026-03-15

### 🎉 E3800LFP Full Telemetry — Data Gap Solved
Reverse-engineered the Pecron mobile app's communication protocol by capturing Android logcat via ADB. Discovered two critical differences between the app and our monitor:

1. **`high_frequency_reporting=3` (LAN+WiFi)** — We were sending `1` (LAN only), which only enables high-frequency data over local TCP. Mode `3` tells the device to also relay all three packet types through cloud MQTT, including `host_packet_data_jdb` (voltage, current, temperature). This was the root cause of missing telemetry.
2. **Continuous re-request** — The app re-sends the high-freq request every ~15-20 seconds. We were disabling after 60s. Now we re-request every 20s to match app behavior.

E3800LFP now reliably reports: battery %, voltage, current, temperature, inverter temp, charging plate temp, per-port power breakdown, remaining time — all via cloud MQTT. Local TCP remains available as a separate transport.

### Fixed
- **Pack status enum not swapped as battery %** — Status values 0-4 are operational states (no charge, cascade, balance, no connection), not percentages. Only swap when value ≥ 5.
- **Crash when `charging_pack_status` is a string** (v0.5.8) — E3800 firmware sends pack fields as strings, now safely cast.
- **Polling interval drift** (v0.5.7) — Cooldown only applies after failed attempts, reduced from 2s to 1s.
- **EP3000 battery field swap** (v0.5.7) — Auto-detects and corrects `charging_pack_battery`/`charging_pack_status` swap.

## [0.5.8] — 2026-03-15

### Fixed
- **Crash when `charging_pack_status` is a string** — E3800LFP firmware sends pack fields as strings (e.g. `"99"` instead of `99`), causing `TypeError` in the battery field swap logic. All pack field comparisons now safely cast to numeric types. (Reported by JaredC01 on #15)

## [0.5.7] — 2026-03-15

### Fixed
- **Polling interval drift (10-30s instead of 5s)** — Fixed three root causes:
  - Reduced connection cooldown from 2.0s to 1.0s and made it smarter (only applies after failures, not on every poll)
  - Reduced TCP socket timeout from 5.0s to 3.0s (3s is sufficient for inter-packet gaps)
  - Cooldown now only skips connection attempts if the PREVIOUS attempt FAILED (not on every successful poll)
- **EP3000 charging_pack_battery field swap** — Some devices report battery percentage in `charging_pack_status` instead of `charging_pack_battery`; monitor now detects and swaps these fields when battery=0 and status=1-100% (applies to ALL data sources, not just local)

## [0.5.6] — 2026-03-02

### Fixed
- **LAN scan crash during setup** — `lan_scan.py` imported `get_auth_key` from `cloud_api` instead of `local_transport`, causing `ImportError` when running network discovery in the setup wizard (#12)
- **6 broken test mock targets** — `test_local_fix.py` patch decorators still pointed at `pecron_monitor.*` after the v0.5.5 modularization; updated to `monitor.*` (12/12 tests passing)

## [0.5.5] — 2026-03-02

### Fixed
- **Home Assistant bridge publishes 0W power on local TCP** — `publish_state()` now uses per-device state caching so partial payload shapes don't zero-out sensors. Computed power values from AC+DC subfields are correctly preserved across polling cycles (#10)
- **Bogus remaining time sent to HA** — remaining time now formatted as human-readable `Xh XXm` / `Xd XXh XXm` and respects the unreliability check from local TCP
- **Duplicate `_truthy()` function** — consolidated into a single robust implementation
- **`packs` variable removed prematurely** — restored charging pack display in `--status` output

### Added
- **Host Battery vs SOC battery** — HA now exposes both `host_percent` (from `host_packet_data_jdb`) and `soc_percent` (overall battery) as separate sensors
- **State caching for HA** — prevents sensor flapping when device alternates between host-packet and overall-packet payload shapes
- **Optimistic switch mode** — AC/DC/UPS switches in HA now use `assumed_state: true` for faster UI feedback when toggling controls (contributed by @Technickly90)
- Fallback sensor paths for switch states (`host_packet_data_jdb` nested variants)

### Refactored
- **Modularized codebase** — split 2185-line monolith into 8 focused modules: `helpers.py`, `constants.py`, `cloud_api.py`, `protocol.py`, `ha_bridge.py`, `monitor.py`, `lan_scan.py`, `setup_wizard.py`, with `pecron_monitor.py` as a thin CLI entry point. No logic changes.

### Contributors
- @Technickly90 — Home Assistant field fixes, state caching design, optimistic switches (#11)

## [0.5.4] — 2026-02-27

### Fixed
- **Local TCP returns zeros for aggregate fields** — device firmware doesn't compute `battery_percentage` locally (server-side only); monitor now falls back to `host_packet_electric_percentage` when top-level value is 0
- **`remain_time` unreliable from local TCP** — shows suspiciously low values (e.g., 4 minutes when battery is 96%); monitor now detects and marks these as "N/A (unreliable from local)" in status display
- **Local/BLE data sources misidentified** — when both local and cloud transports are active, cloud MQTT could overwrite the source label; now preserves local source designation when local data arrives first
- Log output formatting improved: remain time shows "N/A" for invalid values instead of attempting to format negative numbers

## [0.5.3] — 2026-07-27

### Fixed
- **Local TCP connection drops every 60s** — Pecron devices close TCP after each response; monitor now reconnects cleanly on each poll cycle instead of logging errors (#6)
- **`--status` shows "CLOUD MQTT" when local TCP data was received** — local transport source is now preserved when async MQTT data arrives afterward (#6)
- **Local TCP shows 0W input power on some models (F3000LFP)** — total input/output power now computed from AC+DC components as fallback when top-level values are missing (#6)
- Reduced log noise: repeated TCP connect/handshake messages on each poll cycle downgraded to DEBUG level

## [0.5.2] — 2026-02-27

### Fixed
- Local TCP transport never initialized when running `--status` or default monitoring with `lan_ip` configured — only worked with `--local` flag (#6)
- `--local` (offline) mode triggered spurious cloud login every poll cycle due to token refresh check, causing OFFLINE warnings and dropping the local connection (#6)
- `force_offline` flag not preserved during token refresh in `run()` loop, allowing `--local` sessions to switch to cloud mode (#6)
- Potential crash when `mqtt_client` is `None` during token refresh cleanup

### Added
- Unit tests for local transport setup and offline mode behavior

## [0.5.1] — 2026-02-25

### Added
- `--no-ble` flag to disable Bluetooth transport entirely
- Per-device `ble: false` config option to disable BLE for specific devices
- Log message when BLE is disabled

### Fixed
- E300LFP AC output being toggled off intermittently when BLE is enabled (#3) — BLE connection appears to cause firmware side effects on some models; `--no-ble` or `ble: false` provides a workaround

## [0.5.0] — 2026-02-25

### Added
- **Offline/local-only mode** (`--local`) — run without any internet after initial setup
- Automatic offline fallback when cloud login fails but local credentials are cached
- TSL (controls metadata) caching in config.yaml during setup
- Manual LAN IP entry in setup wizard (always offered, not just during LAN scan)
- Data source logging — every reading shows `[via LOCAL TCP]`, `[via BLE]`, `[via CLOUD MQTT]`, or `[via REST API]`
- Status display shows `Connection:` method per device
- `--version` flag
- "Offline / Local-Only Mode" section in README

### Fixed
- `lan_ip` not saved to config.yaml during setup (#1)
- Script always requiring cloud login even with local credentials (#1)

## [0.4.0] — 2026-02-24

### Added
- REST API fallback for device data (same method as ha-pecron HACS addon)
- Device online status check at startup
- `--diagnose` flag for troubleshooting connectivity
- `--controls` flag to list all available controls from TSL
- `--control CODE VALUE` for setting any control by code name
- Manual product selection in setup wizard (option 2)
- `getAuthKey` tried before `regenerateAuthKey` (fixes permission errors on some models)

### Fixed
- E300LFP sensor data not displaying — battery, voltage, temperature (#3)
- Duplicate product keys causing "device is not bound" (4007) errors
- Automation rules firing on invalid battery data (-1%)
- Device Code vs Device Key confusion in docs and setup

## [0.3.0] — 2026-02-22

### Added
- **Bluetooth Low Energy (BLE) transport** — monitor with zero network infrastructure
- BLE scanning in setup wizard
- BLE auto-detection by device key suffix

## [0.2.0] — 2026-02-21

### Added
- **Local WiFi TCP transport** (port 6607, AES-CBC encrypted)
- LAN device scanning in setup wizard
- Auth key caching for offline TCP operation
- Automatic fallback: BLE → WiFi TCP → Cloud MQTT

## [0.1.0] — 2026-02-20

### Added
- Initial release
- Cloud MQTT monitoring via Quectel IoT platform
- AC/DC output control
- Automation rules (battery level, input power, schedule)
- Home Assistant MQTT bridge with auto-discovery
- Telegram, ntfy, and webhook alerts
- Multi-device support
- Auto-detect device model from product catalog
- Systemd service file for 24/7 operation
- Comprehensive README with FAQ and use cases
