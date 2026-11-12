# CLAUDE.md — CamouDesk Development Guide

## Project Overview
CamouDesk is an open-source macOS (`arm64`) anti-detect browser profile manager built with **Camoufox** (`>=0.5.9`), **Playwright**, and **PyWebView** (Cocoa/WebKit), packaged as a native `.app` bundle via an embedded Python C launcher (`macos/launcher.c`) and a bundled relocatable CPython.

## Layout
- `src/camoudesk/models/profile.py` — `Profile` (incl. cached `proxy_exit_ip`), `ProxyConfig` (`masked_display()`, `to_firefox_prefs()`), `FingerprintSummary`, strict ID validation (`sanitize_profile_id`).
- `src/camoudesk/core/storage.py` — `ProfileStore`: atomic writes to `~/Library/Application Support/CamouDesk/registry.json` (overridable via `CAMOUDESK_HOME`) under a cross-process `flock`; isolated `profiles/<id>/firefox_data/`, `fingerprint.json`, `run.json`.
- `src/camoudesk/core/fingerprint.py` — Deterministic `fpgen` fingerprint generation and persistence per profile.
- `src/camoudesk/core/launcher.py` — `build_camoufox_kwargs` (shared by both modes) and `build_launch_spec`, which computes the Camoufox identity in-process via `camoufox.utils.launch_options` and returns the binary, args and env for a direct launch.
- `src/camoudesk/core/proxy_forwarder.py` — Loopback `ProxyForwarder` that authenticates to the upstream proxy for direct-mode browsers (HTTP `Proxy-Authorization`, SOCKS5 RFC 1929, remote DNS, upstream 407 → 502).
- `src/camoudesk/core/browser_worker.py` — Playwright-based persistent Camoufox worker (headless/automation).
- `src/camoudesk/manager/process_manager.py` — Launch modes (`direct` default for headed, `worker` for headless), per-profile forwarders for proxy credentials, `run.json` re-adoption after GUI restart (restarting forwarders on their recorded port), per-profile logs (`logs/<id>.log`), graceful process-group stop.
- `src/camoudesk/ui/` — PyWebView Cocoa host (`app.py`), typed JS-Python bridge (`api.py`), and offline dark UI (`frontend/`).
- `macos/` — `.app` builder (`launcher.c`, `generate_icon.py`, `build_app.sh`, `build_dmg.sh`).
- `scripts/bench_profiles.py` — Launch-latency and per-profile memory benchmark (`--mode worker|direct`, `--headed`).

## Important Technical Invariants
1. **Never use `input()` in `browser_worker.py`**: Workers are spawned with `stdin=subprocess.DEVNULL`. Always wait on Playwright context/page lifecycle events and `SIGINT`/`SIGTERM`.
2. **Keep `canvas_noise=True` default in `build_camoufox_kwargs`** (`core/launcher.py`): Leaving `canvas_noise=None` triggers Camoufox's nested `host_gpu()` subprocess probe inside a thread pool, which can deadlock Juggler pipes when combined with `persistent_context=True` on macOS.
3. **Preserve deterministic fingerprints**: Re-use saved `fingerprint.json` on launch (`i_know_what_im_doing=True`) unless the user explicitly regenerates identity or changes target OS. Direct and worker launches must produce identical `CAMOU_CONFIG_*`/`CAMOU_PREFS_*` (covered by a live test).
4. **Offline frontend**: Keep all CSS/JS/assets in `src/camoudesk/ui/frontend/` with zero external CDN calls.
5. **Never SIGTERM a direct-mode browser first**: Firefox dies instantly on SIGTERM and loses unflushed cookies. Stop with `NSRunningApplication.terminate()` (Apple Event quit), then escalate to SIGTERM/SIGKILL of the whole process group.
6. **Registry writes go through `ProfileStore._locked()`**: worker processes write `registry.json` too. Never overwrite an unreadable registry (`RegistryCorruptError`) and keep entries that fail validation as raw dicts.
7. **Nothing may write inside the `.app` at runtime**: it breaks the code seal and fails on read-only volumes. Camoufox's pinned fpgen model is installed at build time with 2038 mtimes; copy bundles with `ditto` (not `cp -R`) so those mtimes survive.
8. **Proxy credentials never reach Firefox, logs, or `run.json`**: direct-mode browsers with a credentialed proxy talk to a `ProxyForwarder` on 127.0.0.1 inside the GUI process. If the GUI dies, those browsers lose network access until a restarted GUI re-adopts them and re-binds the forwarder on the port stored in `run.json`; quitting the GUI normally stops all browsers. The loopback listener has no auth of its own, so other local processes could use the upstream proxy while that profile runs.

## Commands
- Unit tests (fast, offline): `pytest tests/ -v -m "not live_browser"`
- Full suite (live headless Camoufox: worker + direct cookie persistence, identity parity): `pytest tests/ -v`
- Build self-contained `.app` (no local install): `bash macos/build_app.sh --no-install`
- Fast local-only build linked to the venv: `bash macos/build_app.sh --dev --no-install`
- Build & install to `~/Applications/CamouDesk.app`: `bash macos/build_app.sh`
- Benchmark: `python scripts/bench_profiles.py --count 3 --mode direct`
