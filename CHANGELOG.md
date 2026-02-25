# Changelog

All notable changes to pecron-monitor are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project uses [Semantic Versioning](https://semver.org/).

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
