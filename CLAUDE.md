# CLAUDE.md — CamouDesk Development Guide

## Project Overview
CamouDesk is an open-source macOS (`arm64`) anti-detect browser profile manager built with **Camoufox** (`>=0.5.9`), **Playwright**, and **PyWebView** (Cocoa/WebKit), packaged as a native `.app` bundle via an embedded Python C launcher (`macos/launcher.c`) and a bundled relocatable CPython.

## Layout
- `src/camoudesk/models/profile.py` — `Profile` (folder, tags, `start_urls`, trash `deleted_at`, saved-proxy `proxy_id`, cached inline-proxy location, `fp_overrides`), `ProxyConfig` (`masked_display()`, `to_firefox_prefs()`), sanitizers (`clean_start_urls`, `clean_fp_overrides`, `sanitize_profile_id`).
- `src/camoudesk/models/proxy.py` — `SavedProxy` and `ProxyCheck` (passwords never in `to_public_dict()`).
- `src/camoudesk/core/storage.py` — `ProfileStore`: atomic writes to `~/Library/Application Support/CamouDesk/registry.json` (overridable via `CAMOUDESK_HOME`) under a cross-process `flock`; trash, bulk metadata, clone/duplicate, `insert_profile`; isolated `profiles/<id>/firefox_data/`, `fingerprint.json`, `run.json`.
- `src/camoudesk/core/jsonstore.py`, `proxy_store.py`, `settings_store.py` — shared atomic/flock helpers; `proxies.json` (0600) and `settings.json`.
- `src/camoudesk/core/fingerprint.py` — Deterministic `fpgen` fingerprint generation (verified for OS coherence and pinned screen size) and persistence per profile.
- `src/camoudesk/core/launcher.py` — `resolve_network` (saved/inline proxy, "auto" timezone/locale, exit IP), `profile_camoufox_kwargs` (single kwargs builder for every launch path), `build_launch_spec` (direct launch: binary, args, minimal env), GPU/screen option lists.
- `src/camoudesk/core/search_engine.py` — Address-bar search engine (Settings → Search) written into each profile's `search.json.mozlz4` before launch, plus prefs that stop Camoufox's SearchEngines policy (which defaults to a placeholder engine on 127.0.0.1) from removing it.
- `src/camoudesk/core/proxy_forwarder.py` — Loopback `ProxyForwarder` that authenticates to the upstream proxy for direct-mode browsers (HTTP `Proxy-Authorization`, SOCKS5 RFC 1929, remote DNS, upstream 407 → 502).
- `src/camoudesk/core/proxy.py`, `proxy_parse.py`, `geo.py`, `geoip_db.py` — proxy checks, provider-format parser, country → locale (deterministic), GeoIP database status/download.
- `src/camoudesk/core/cookies.py` — cookie parsers (extension JSON, Playwright, Netscape), sqlite reader, exporters.
- `src/camoudesk/core/profile_archive.py` — `.camoudesk` export/import (untrusted-archive checks).
- `src/camoudesk/core/api_server.py` — local REST API; `src/camoudesk/sdk.py` — Python SDK (`open_profile`).
- `src/camoudesk/core/browser_worker.py` — Playwright-based persistent Camoufox worker (headless runs, cookie import).
- `src/camoudesk/manager/process_manager.py` — Launch modes (`direct` for headed, `worker` for headless; `sdk` runs are adopted), per-profile forwarders, `run.json` re-adoption and periodic rescans, starting/stopping/busy transitions, status push listeners + monitor thread, graceful stop, diagnostics, cookie import/clear.
- `src/camoudesk/ui/` — PyWebView host (`app.py`, serves the UI via pywebview's loopback HTTP server), bridge (`api.py`, prefixed methods with a strict `{ok, data|error}` envelope), frontend (`frontend/`: Preact + htm vendored in `vendor/`, `lib/` store/actions, `components/`, `dev/mock-api.js`).
- `macos/` — `.app` builder (`launcher.c`, `generate_icon.py`, `build_app.sh`, `build_dmg.sh`).
- `scripts/` — `bench_profiles.py` (launch/memory benchmark), `dev_server.py` (no-cache frontend server for `?mock`).

## Important Technical Invariants
1. **Never use `input()` in `browser_worker.py`**: Workers are spawned with `stdin=subprocess.DEVNULL`. Always wait on Playwright context/page lifecycle events and `SIGINT`/`SIGTERM`.
2. **Keep `canvas_noise=True` default in `build_camoufox_kwargs`** (`core/launcher.py`): Leaving `canvas_noise=None` triggers Camoufox's nested `host_gpu()` subprocess probe inside a thread pool, which can deadlock Juggler pipes when combined with `persistent_context=True` on macOS.
3. **Preserve deterministic fingerprints**: Re-use saved `fingerprint.json` on launch (`i_know_what_im_doing=True`) unless the user explicitly regenerates identity or changes target OS. All launch paths build kwargs through `profile_camoufox_kwargs`; direct and worker launches must produce identical `CAMOU_CONFIG_*`/`CAMOU_PREFS_*`, with and without overrides (live tests). Never trust `fpgen` output blindly: verify OS coherence and pinned screen size, and never silently replace a saved fingerprint.
4. **Offline frontend**: Keep all CSS/JS/assets in `src/camoudesk/ui/frontend/` with zero external CDN calls. Vendored libraries are pinned with sha256 in `frontend/vendor/README.md`.
5. **Never SIGTERM a direct-mode browser first**: Firefox dies instantly on SIGTERM and loses unflushed cookies. Stop with `NSRunningApplication.terminate()` (Apple Event quit), then escalate to SIGTERM/SIGKILL of the whole process group. SDK runs are signalled by PID only.
6. **Registry writes go through `ProfileStore._locked()`** (other stores through `jsonstore.locked`): worker processes and SDK scripts write too. Never overwrite an unreadable store and keep entries that fail validation as raw dicts.
7. **Nothing may write inside the `.app` at runtime**: it breaks the code seal and fails on read-only volumes. Camoufox's pinned fpgen model is installed at build time with 2038 mtimes; copy bundles with `ditto` (not `cp -R`) so those mtimes survive.
8. **Proxy credentials never reach Firefox, logs, `run.json`, the UI, or exports by default**: direct-mode browsers with a credentialed proxy talk to a `ProxyForwarder` on 127.0.0.1 inside the GUI process. If the GUI dies, those browsers lose network access until a restarted GUI re-adopts them and re-binds the forwarder on the port stored in `run.json`. The loopback listener has no auth of its own, so other local processes could use the upstream proxy while that profile runs.
9. **Never fall back to the real connection**: a deleted saved proxy blocks the launch; "spoof WebRTC" without GeoIP data blocks WebRTC; the forwarder never connects directly; `network.proxy.failover_direct` is false.
10. **Browsers get a minimal environment** (`launcher.ENV_ALLOWLIST` plus Camoufox's own variables); never pass the GUI's environment through.
11. **Treat imported archives as untrusted** (`profile_archive`): confine member paths, reject symlinks, cap size/count, stage before moving into place.

## Commands
- Unit tests (fast, offline): `pytest tests/ -v -m "not live_browser"`
- Full suite (adds live headless Camoufox tests): `pytest tests/ -v`
- UI with fake data: `python scripts/dev_server.py` then open `http://127.0.0.1:5178/index.html?mock` (or the `frontend-mock` launch config)
- Build self-contained `.app` (no local install): `bash macos/build_app.sh --no-install`
- Fast local-only build linked to the venv: `bash macos/build_app.sh --dev --no-install`
- Build & install to `~/Applications/CamouDesk.app`: `bash macos/build_app.sh`
- Benchmark: `python scripts/bench_profiles.py --count 3 --mode direct`
- After changing Python code, quit and reopen a running app: an old process serving a new frontend fails to load profiles.
