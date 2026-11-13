# CamouDesk

An open-source macOS anti-detect browser profile manager powered by **Camoufox** (a hardened Firefox) and **PyWebView**. Manage isolated browser identities, proxies and cookies on Apple Silicon (`arm64`), with everything stored locally on your Mac.

## Features

- **Profiles**: each profile is a separate browser with its own fingerprint, cookies, storage and network. Folders, coloured tags, search and filters, bulk start/stop/assign proxy/move/tag/export, trash with restore, clone (new fingerprint) or duplicate (same device and cookies).
- **Proxies**: HTTP, HTTPS, SOCKS4 and SOCKS5, with or without credentials. Save proxies once and reuse them; bulk import provider lists (`host:port:user:pass`, `user:pass@host:port`, ...); check all with exit IP, location and latency; rotate IP via a provider's change-IP URL.
- **Evomi integration**: paste an Evomi residential proxy once and every profile using it gets its own session, so its own stable IP. Choose country, region and city per profile; pick the session type (sticky with a lifetime of up to 24 h, hard, locked or rotating); "New IP" gives a profile a fresh session. An optional Evomi API key (Settings → Integrations) loads Evomi's official region and city lists.
- **Network matching**: "Auto" timezone and language follow the proxy's exit location (or your Mac when there is no proxy). With the optional GeoIP database, geolocation and WebRTC match the proxy IP too.
- **Fingerprints**: a realistic, consistent identity per profile that stays the same on every launch. Pick the screen size and graphics card (only devices Firefox has really been seen on), WebRTC mode, geolocation behaviour, image and WebGL blocking.
- **Cookies**: import (browser-extension JSON, Playwright, Netscape `cookies.txt`), export, clear.
- **Portable profiles**: export to `.camoudesk` files and import on another Mac (proxy passwords excluded unless you choose otherwise).
- **Automation**: a token-protected local REST API and a Python SDK for Playwright scripts.

## Quick Start (Development)

```bash
pip install -e ".[dev]"
camoufox fetch
camoudesk-gui
```

## Automation

Enable the REST API in **Settings → Automation** to get a token, then:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:35120/v1/profiles
```

Endpoints: `GET /v1/health`, `GET|POST /v1/profiles`, `GET|PATCH /v1/profiles/{id}`, `POST /v1/profiles/{id}/start` (`{"headless": true}`), `POST /v1/profiles/{id}/stop`, `GET /v1/profiles/{id}/status`, `POST /v1/profiles/{id}/new-ip` (new Evomi session), `GET /v1/proxies`, `POST /v1/proxies/{id}/check`. The API only listens on 127.0.0.1.

For full browser automation, use the SDK from your own script (the profile appears as running in the app):

```python
from camoudesk.sdk import open_profile

with open_profile("PROFILE_ID") as context:
    page = context.new_page()
    page.goto("https://example.com")
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
pytest tests/ -v -m "not live_browser"   # fast, offline
pytest tests/ -v                         # adds live headless Camoufox tests
```

## UI development

```bash
python scripts/dev_server.py
```

Then open `http://127.0.0.1:5178/index.html?mock` to work on the interface with fake data.
