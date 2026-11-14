# CLAUDE.md — CamouDesk Development Guide

## Project Overview
CamouDesk is an open-source macOS (`arm64`) anti-detect browser profile manager built with **Camoufox** (`>=0.5.9`), **Playwright**, and **PyWebView** (Cocoa/WebKit), packaged as a native `.app` bundle via an embedded Python C launcher (`macos/launcher.c`) and a bundled relocatable CPython.

## Architecture
```
UI bridge (ui/api.py) ─┐
REST API (core/api_server.py) ┼─► services/ (Profile, Proxy, Launch, Identity, Cookie, Archive, Settings)
CLI / SDK / worker ────┘                │                │                 │
                         engines/ (EngineAdapter)  core/providers/   stores (JSON + flock)
                         └ camoufox.py             └ evomi.py
            core/events.py (status · proxies · theme · settings) → UI pushes, window chrome, API server
```
- `src/camoudesk/services/` — All business logic. `context.py` → `AppContext` builds the stores, `ProcessManager`, `EventBus`, engine and services once per process; interfaces only call services. `errors.py` (`NotFound`/`Invalid`/`Conflict`, mapped to the bridge envelope and to HTTP 404/400/409), `views.py` (profile/proxy rows, never credentials).
- `src/camoudesk/engines/` — `EngineAdapter` (`base.py`): identity generation/verification/summary, host preparation, canvas policy, context kwargs (worker/SDK), direct launch spec, binary status, GPU/screen options. `camoufox.py` delegates to `core/fingerprint.py`, `core/launcher.py`, `core/host_gpu.py`. Services, the process manager, the worker and the SDK reach the engine only through `ctx.engine` / `get_engine()`.
- `src/camoudesk/core/events.py` — `EventBus`; services publish, the GUI host subscribes.
- `src/camoudesk/manager/` — `process_manager.py` (`ProcessManager` facade: launch paths) composed of mixins: `runs.py` (run.json, re-adoption, status), `monitor.py` (listeners, monitor thread), `forwarders.py` (loopback forwarders, proxy log lines), `stopper.py` (graceful stop, invariant 5), `tasks.py` (cookie import, anonymity check, logs, diagnostics); `constants.py` (modes, worker env).

## Layout
- `src/camoudesk/models/profile.py` — `Profile` (folder, tags, `start_urls`, trash `deleted_at`, saved-proxy `proxy_id`, cached inline-proxy location, `fp_overrides`), `ProxyConfig` (`masked_display()`, `to_firefox_prefs()`), sanitizers (`clean_start_urls`, `clean_fp_overrides`, `sanitize_profile_id`).
- `src/camoudesk/models/proxy.py` — `SavedProxy` and `ProxyCheck` (passwords never in `to_public_dict()`).
- `src/camoudesk/core/storage.py` — `ProfileStore`: atomic writes to `~/Library/Application Support/CamouDesk/registry.json` (overridable via `CAMOUDESK_HOME`) under a cross-process `flock`; trash, bulk metadata, clone/duplicate, `insert_profile`; isolated `profiles/<id>/firefox_data/`, `fingerprint.json`, `run.json`.
- `src/camoudesk/core/jsonstore.py`, `proxy_store.py`, `settings_store.py` — shared atomic/flock helpers; `proxies.json` (0600) and `settings.json`.
- `src/camoudesk/core/fingerprint.py` — Per-profile identity: `fpgen` samples (verified for OS coherence and pinned screen size) or Camoufox's recorded real-device presets (`PRESET_MARK`, launched as `fingerprint_preset`), previews (`generate_verified_fingerprint`/`generate_preset_fingerprint`) and persistence.
- `src/camoudesk/core/host_gpu.py` — Host GPU probe stored per Camoufox binary, for the canvas policy (invariant 2).
- `src/camoudesk/core/identity_probe.html`, `identity_check.py` — Anonymity check: `browser_worker --check-identity` opens the profile headless through its own network, runs the probe page in the main world (navigator/screen/Intl/WebGL/canvas/audio/fonts/WebRTC/exit IP, re-read in a Worker and an iframe, native-getter checks) and prints it with Camoufox's claimed identity; `evaluate()` grades it into `profiles/<id>/identity_check.json` (no credentials).
- `src/camoudesk/core/launcher.py` — `resolve_network` (saved/inline proxy, "auto" timezone/locale, exit IP), `profile_camoufox_kwargs` (single kwargs builder for every launch path), `build_launch_spec` (direct launch: binary, args, minimal env), GPU/screen option lists.
- `src/camoudesk/core/search_engine.py` — Address-bar search engine (Settings → Search) written into each profile's `search.json.mozlz4` before launch, plus prefs that stop Camoufox's SearchEngines policy (which defaults to a placeholder engine on 127.0.0.1) from removing it.
- `src/camoudesk/core/providers/` — `ProviderAdapter` interface and registry (`detect(host)`, `get(name)`); `evomi.py` registers `EvomiAdapter`. Evomi integration: a saved Evomi proxy keeps the base password plus targeting/session defaults (`SavedProxy.provider`); each profile composes its own session ID (`session_id(profile_id, rotation)`, deterministic, so every launch path gets the same credentials) with per-profile overrides (`Profile.provider_opts`). Its exit location is cached on the profile under `provider_opts.checked` and re-checked at every launch (`launcher.ensure_location`). Optional API key in `providers.json` (0600), targeting lists cached a day.
- `src/camoudesk/core/proxy_forwarder.py` — Loopback `ProxyForwarder` that authenticates to the upstream proxy for direct-mode browsers (HTTP `Proxy-Authorization`, SOCKS5 RFC 1929, remote DNS, upstream 407 → 502).
- `src/camoudesk/core/proxy.py`, `proxy_parse.py`, `geo.py`, `geoip_db.py` — proxy checks, provider-format parser, country → locale (deterministic), GeoIP database status/download.
- `src/camoudesk/core/cookies.py` — cookie parsers (extension JSON, Playwright, Netscape), sqlite reader, exporters.
- `src/camoudesk/core/profile_archive.py` — `.camoudesk` export/import (untrusted-archive checks).
- `src/camoudesk/core/api_server.py` — local REST API; `src/camoudesk/sdk.py` — Python SDK (`open_profile`).
- `src/camoudesk/core/browser_worker.py` — Playwright-based persistent Camoufox worker (headless runs, cookie import).
- `src/camoudesk/manager/` — Launch modes (`direct` for headed, `worker` for headless; `sdk` runs are adopted), per-profile forwarders, `run.json` re-adoption and periodic rescans, starting/stopping/busy transitions, status listeners + monitor thread, graceful stop, diagnostics, cookie import/clear, anonymity check (see Architecture).
- `src/camoudesk/ui/` — PyWebView host (`app.py`, builds the `AppContext`, serves the UI via pywebview's loopback HTTP server, subscribes UI pushes to the event bus), thin bridge (`api.py`, prefixed methods calling services with a strict `{ok, data|error}` envelope; only its own methods may be exposed to the page — stores/manager/context are `_serializable = False`, enforced by `tests/test_js_api_surface.py`), frontend (`frontend/`: Preact + htm vendored in `vendor/`, `lib/` store/actions, `components/`, `dev/mock-api.js`).
- `macos/` — `.app` builder (`launcher.c`, `generate_icon.py`, `build_app.sh`, `build_dmg.sh`).
- `scripts/` — `bench_profiles.py` (launch/memory benchmark), `dev_server.py` (no-cache frontend server for `?mock`).

## Important Technical Invariants
1. **Never use `input()` in `browser_worker.py`**: Workers are spawned with `stdin=subprocess.DEVNULL`. Always wait on Playwright context/page lifecycle events and `SIGINT`/`SIGTERM`.
2. **Canvas policy lives in `launcher.canvas_policy` + `core/host_gpu.py`**: Firefox never perturbs canvas pixels, so an identity that claims this host's OS and GPU shows its real canvas (`canvas_noise=None` for fpgen identities, `False` for a recorded preset whose GPU is the host's); every other identity stays covered (`True`). Camoufox's own `host_gpu()` probe must never run inside a launch: it starts a nested Playwright in a thread pool, which deadlocks Juggler pipes of a persistent context on macOS. `host_gpu.ensure()` probes headless outside launches (GUI startup, before direct launches, worker/SDK before `Camoufox()`) and stores the result per binary in `host_gpu.json`; `host_gpu.install()` points Camoufox's probe at it. Never pass `None`/`False` without `real_canvas()` having seen a stored result.
3. **Preserve deterministic fingerprints**: Re-use saved `fingerprint.json` on launch (`i_know_what_im_doing=True`) unless the user explicitly regenerates identity or changes target OS. All launch paths build kwargs through `profile_camoufox_kwargs`; direct and worker launches must produce identical `CAMOU_CONFIG_*`/`CAMOU_PREFS_*`, with and without overrides (live tests). Camoufox redraws a generated `window.screenY` of 0 at random on every launch; `launcher._keep_generated_screen_y` keeps the fingerprint's value. Never trust `fpgen` output blindly: verify OS coherence and pinned screen size, and never silently replace a saved fingerprint.
4. **Offline frontend**: Keep all CSS/JS/assets in `src/camoudesk/ui/frontend/` with zero external CDN calls. Vendored libraries are pinned with sha256 in `frontend/vendor/README.md`.
5. **Never SIGTERM a direct-mode browser first**: Firefox dies instantly on SIGTERM and loses unflushed cookies. Stop with `NSRunningApplication.terminate()` (Apple Event quit), then escalate to SIGTERM/SIGKILL of the whole process group. SDK runs are signalled by PID only.
6. **Registry writes go through `ProfileStore._locked()`** (other stores through `jsonstore.locked`): worker processes and SDK scripts write too. Never overwrite an unreadable store and keep entries that fail validation as raw dicts.
7. **Nothing may write inside the `.app` at runtime**: it breaks the code seal and fails on read-only volumes. Camoufox's pinned fpgen model is installed at build time with 2038 mtimes; copy bundles with `ditto` (not `cp -R`) so those mtimes survive.
8. **Proxy credentials never reach Firefox, logs, `run.json`, the UI, or exports by default**: direct-mode browsers with a credentialed proxy talk to a `ProxyForwarder` on 127.0.0.1 inside the GUI process. If the GUI dies, those browsers lose network access until a restarted GUI re-adopts them and re-binds the forwarder on the port stored in `run.json`. The loopback listener has no auth of its own, so other local processes could use the upstream proxy while that profile runs.
9. **Every launch path runs `launcher.ensure_location` before starting the browser** (GUI direct/worker and the SDK): with "auto" timezone/locale it checks the proxy's exit location, so no path silently uses the host's timezone. A provider proxy's shared check (`SavedProxy.last_check`) never feeds a profile's timezone.
10. **Never fall back to the real connection**: a deleted saved proxy blocks the launch; "spoof WebRTC" without GeoIP data blocks WebRTC; the forwarder never connects directly; `network.proxy.failover_direct` is false.
11. **Browsers get a minimal environment** (`launcher.ENV_ALLOWLIST` plus Camoufox's own variables); never pass the GUI's environment through.
12. **Treat imported archives as untrusted** (`profile_archive`): confine member paths, reject symlinks, cap size/count, stage before moving into place.
13. **Spoofing only inside the engine**: identities are applied by Camoufox's C++ patches (`CAMOU_CONFIG_*`); never inject JavaScript overrides into pages (detectable through `toString`/property descriptors, and they miss workers/iframes). The anonymity check asserts native getters and worker/iframe consistency.
14. **Local-first**: profiles live in `firefox_data/` on this Mac and are reused across launches. Any future sync or team sharing must be opt-in, end-to-end encrypted and carry the whole profile directory.

## Commands
- Lint & types: `ruff check src tests` and `mypy` (strict for `services/`, `engines/`, `models/`, `core/providers/`, `core/events`); `pre-commit install` runs them on commit
- Unit tests (fast, offline): `pytest tests/ -v -m "not live_browser and not ui"`
- UI end-to-end (Camoufox drives the frontend against the mock bridge): `pytest tests/ -v -m ui`
- CI (`.github/workflows/ci.yml`): lint+types, unit, UI e2e, live tests (main, nightly, manual), and a bundle build uploaded as an artifact
- Full suite (adds live headless Camoufox tests): `pytest tests/ -v`
- UI with fake data: `python scripts/dev_server.py` then open `http://127.0.0.1:5178/index.html?mock` (or the `frontend-mock` launch config)
- Build self-contained `.app` (no local install): `bash macos/build_app.sh --no-install`
- Fast local-only build linked to the venv: `bash macos/build_app.sh --dev --no-install`
- Build & install to `~/Applications/CamouDesk.app`: `bash macos/build_app.sh`
- Benchmark: `python scripts/bench_profiles.py --count 3 --mode direct`
- After changing Python code, quit and reopen a running app: an old process serving a new frontend fails to load profiles.
