# 🦊 CamouDesk

An open-source macOS anti-detect profile manager powered by **Camoufox** and **PyWebView**. Designed as a clean, privacy-respecting desktop alternative for managing isolated browser identities and residential proxies on Apple Silicon (`arm64`).

## Architecture

- **`camoudesk.models`**: Strict profile/proxy schema validation (`http`, `https`, `socks5`), masked credential views, and directory traversal guards.
- **`camoudesk.core.fingerprint`**: Generates coherent Firefox hardware fingerprints once per profile via `fpgen` and freezes them in `fingerprint.json` so accounts retain stable device identities across restarts.
- **`camoudesk.core.storage`**: Atomic JSON registry (`~/Library/Application Support/CamouDesk/registry.json`) with isolated per-profile `firefox_data/` directories (`persistent_context=True`).
- **`camoudesk.core.browser_worker`**: Dedicated persistent Camoufox worker process that exits cleanly when all tabs/windows close or on `SIGTERM`.
- **`camoudesk.manager.process_manager`**: Tracks worker PIDs, captures per-profile stdout/stderr logs, and handles graceful teardown.
- **`camoudesk.ui`**: Offline dark-mode desktop UI served inside a native macOS Cocoa/WebKit window via a compiled Mach-O `.app` launcher (`CamouDesk.app`).

## Quick Start (Development)

```bash
pip install -e ".[dev]"
camoufox fetch
python -m camoudesk.ui.app
```

## Build macOS `.app` Bundle

```bash
bash macos/build_app.sh
```

This generates `AppIcon.icns`, compiles the native Mach-O launcher (`macos/launcher.c`), validates `Info.plist`, signs the bundle ad-hoc, and installs `~/Applications/CamouDesk.app`.

To create a distributable `.dmg`:

```bash
bash macos/build_dmg.sh
```

## Run Tests

```bash
pytest tests/ -v
```
