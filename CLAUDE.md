# CLAUDE.md — CamouDesk Development Guide

## Project Overview
CamouDesk is an open-source macOS (`arm64`) anti-detect browser profile manager built with **Camoufox** (`>=0.5.9`), **Playwright**, and **PyWebView** (Cocoa/WebKit), packaged as a native `.app` bundle via an embedded Python C launcher (`macos/launcher.c`).

## Layout
- `src/camoudesk/models/profile.py` — `Profile`, `ProxyConfig` (with `masked_display()`), `FingerprintSummary`, strict ID validation (`sanitize_profile_id`).
- `src/camoudesk/core/storage.py` — `ProfileStore` using atomic temp-file writes to `~/Library/Application Support/CamouDesk/registry.json` (overridable via `CAMOUDESK_HOME`) and isolated `profiles/<id>/firefox_data/` + `fingerprint.json`.
- `src/camoudesk/core/fingerprint.py` — Deterministic `fpgen` fingerprint generation and persistence per profile.
- `src/camoudesk/core/browser_worker.py` — Persistent Camoufox worker process.
- `src/camoudesk/manager/process_manager.py` — Detached worker PID tracking, per-profile log capture (`logs/<id>.log`), and graceful stop.
- `src/camoudesk/ui/` — PyWebView Cocoa host (`app.py`), typed JS-Python bridge (`api.py`), and offline dark UI (`frontend/`).
- `macos/` — Native Mach-O `.app` bundle builder (`launcher.c`, `generate_icon.py`, `build_app.sh`, `build_dmg.sh`).

## Important Technical Invariants
1. **Never use `input()` in `browser_worker.py`**: Workers are spawned with `stdin=subprocess.DEVNULL`. Always wait on Playwright context/page lifecycle events and `SIGINT`/`SIGTERM`.
2. **Keep `canvas_noise=True` default in `build_camoufox_kwargs`**: Leaving `canvas_noise=None` triggers Camoufox's nested `host_gpu()` subprocess probe inside a thread pool, which can deadlock Juggler pipes when combined with `persistent_context=True` on macOS.
3. **Preserve deterministic fingerprints**: Re-use saved `fingerprint.json` on launch (`i_know_what_im_doing=True`) unless the user explicitly regenerates identity or changes target OS.
4. **Offline frontend**: Keep all CSS/JS/assets in `src/camoudesk/ui/frontend/` with zero external CDN calls.

## Commands
- Unit tests (fast, offline): `pytest tests/ -v -m "not live_browser"`
- Full suite (includes live headless Camoufox loopback cookie test): `pytest tests/ -v`
- Build `.app` bundle (no local install): `bash macos/build_app.sh --no-install`
- Build & install to `~/Applications/CamouDesk.app`: `bash macos/build_app.sh`
