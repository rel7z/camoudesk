# 🦊 CamouDesk

An open-source macOS anti-detect profile manager powered by **Camoufox** and **PyWebView**. Designed as a clean, privacy-respecting desktop alternative for managing isolated browser identities and residential proxies on Apple Silicon (`arm64`).

## Architecture

- **`camoudesk.models`**: Strict profile/proxy schema validation (`http`, `https`, `socks5`), masked credential views, and directory traversal guards.
- **`camoudesk.core.fingerprint`**: Generates coherent Firefox hardware fingerprints once per profile via `fpgen` and freezes them in `fingerprint.json` so accounts retain stable device identities across restarts.
- **`camoudesk.core.storage`**: Atomic JSON registry (`~/Library/Application Support/CamouDesk/registry.json`) with isolated per-profile `firefox_data/` directories (`persistent_context=True`).
- **`camoudesk.core.launcher`**: Computes each profile's Camoufox identity in-process and starts the browser binary directly (no Playwright driver or Python worker per profile), about 3x faster to first page.
- **`camoudesk.core.proxy_forwarder`**: Loopback forwarder that answers HTTP/SOCKS5 proxy authentication for directly launched browsers, so credentials never reach Firefox.
- **`camoudesk.core.browser_worker`**: Playwright-based persistent worker, used for headless/automation runs.
- **`camoudesk.manager.process_manager`**: Chooses the launch mode, re-adopts running browsers after a GUI restart (`run.json`), captures per-profile logs, and stops browsers gracefully (Cmd+Q-style quit before signals) so cookies are flushed.
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

This bundles a pinned, checksum-verified relocatable CPython (python-build-standalone) with all dependencies, generates `AppIcon.icns`, compiles the native Mach-O launcher (`macos/launcher.c`), validates `Info.plist`, signs the bundle ad-hoc, and installs `~/Applications/CamouDesk.app`. The bundle runs on Macs without Python installed; the Camoufox browser itself is still fetched per user (`camoufox fetch`).

For a quick local build linked against your venv (not distributable): `bash macos/build_app.sh --dev`.

To create a distributable `.dmg`:

```bash
bash macos/build_dmg.sh
```

## Run Tests

```bash
pytest tests/ -v
```
