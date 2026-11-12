"""Standalone Camoufox persistent browser worker executed per active profile."""

import argparse
import signal
import sys
import threading
from typing import Any, Optional

from camoufox.sync_api import Camoufox

from camoudesk.core.launcher import build_camoufox_kwargs, profile_camoufox_kwargs, resolve_network
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.storage import ProfileStore

__all__ = ["build_camoufox_kwargs", "run_worker", "main"]


def run_worker(
    profile_id: str,
    headless: bool = False,
    url_override: Optional[str] = None,
    max_seconds: Optional[float] = None,
) -> int:
    """Launch Camoufox for profile_id and block until browser closes or signal received."""
    store = ProfileStore()
    profile = store.get_profile(profile_id)
    if profile is None:
        print(f"[ERROR] Profile '{profile_id}' not found.", file=sys.stderr, flush=True)
        return 2

    network = resolve_network(profile, ProxyStore(store.app_dir))
    if network.missing_saved_proxy:
        print("[ERROR] This profile's saved proxy was deleted; choose another proxy or Direct.", file=sys.stderr, flush=True)
        return 2
    store.mark_used(profile.id)

    stop_event = threading.Event()
    closed_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        print(f"[INFO] Signal {signum} received; shutting down browser cleanly.", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    target_urls = [url_override.strip()] if url_override and url_override.strip() else list(profile.start_urls)
    kwargs, _geoip = profile_camoufox_kwargs(profile, store, network, headless)

    print(
        f"[INFO] Launching Camoufox profile={profile.id} os={profile.os} headless={headless}",
        flush=True,
    )

    try:
        with Camoufox(**kwargs) as context:
            context.on("close", lambda *_: closed_event.set())
            pages = list(context.pages)
            for i, target_url in enumerate(target_urls):
                page = pages[0] if (i == 0 and pages) else context.new_page()
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as nav_err:
                    print(f"[WARN] Initial navigation warning for {target_url}: {nav_err}", flush=True)

            if max_seconds is not None and max_seconds <= 0:
                return 0

            elapsed = 0.0
            step_ms = 250
            while not stop_event.is_set() and not closed_event.is_set():
                current_pages = [p for p in context.pages if not p.is_closed()]
                if not current_pages:
                    # Wait briefly on macOS in case user opened a fresh window right away.
                    stop_event.wait(0.5)
                    if not [p for p in context.pages if not p.is_closed()]:
                        break
                    current_pages = [p for p in context.pages if not p.is_closed()]
                try:
                    current_pages[0].wait_for_timeout(step_ms)
                except Exception:
                    break
                elapsed += step_ms / 1000.0
                if max_seconds is not None and elapsed >= max_seconds:
                    break
    except Exception as exc:
        print(f"[ERROR] Camoufox launch failed: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"[INFO] Profile {profile.id} closed cleanly.", flush=True)
    return 0


def run_cookie_import(profile_id: str, cookie_file: str) -> int:
    """Write cookies into the profile through Firefox itself (headless), then exit.

    Prints one ``[RESULT] {json}`` line with the number of cookies the profile now holds.
    """
    import json

    store = ProfileStore()
    profile = store.get_profile(profile_id)
    if profile is None:
        print(f"[ERROR] Profile '{profile_id}' not found.", file=sys.stderr, flush=True)
        return 2
    try:
        with open(cookie_file, "r", encoding="utf-8") as fh:
            cookies = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] Could not read cookies: {exc}", file=sys.stderr, flush=True)
        return 2
    network = resolve_network(profile, ProxyStore(store.app_dir))
    kwargs, _ = profile_camoufox_kwargs(profile, store, network, headless=True)
    try:
        with Camoufox(**kwargs) as context:
            before = len(context.cookies())
            context.add_cookies(cookies)
            after = len(context.cookies())
    except Exception as exc:
        print(f"[ERROR] Cookie import failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print("[RESULT] " + json.dumps({"requested": len(cookies), "before": before, "after": after}), flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="CamouDesk Persistent Browser Worker")
    parser.add_argument("--profile-id", required=True, help="Target profile ID")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--url", help="Override startup URL")
    parser.add_argument("--max-seconds", type=float, default=None, help="Optional test lifetime limit")
    parser.add_argument("--import-cookies", metavar="FILE", help="Add cookies from a JSON file, then exit")
    args = parser.parse_args()
    if args.import_cookies:
        raise SystemExit(run_cookie_import(args.profile_id, args.import_cookies))
    raise SystemExit(
        run_worker(
            profile_id=args.profile_id,
            headless=args.headless,
            url_override=args.url,
            max_seconds=args.max_seconds,
        )
    )


if __name__ == "__main__":
    main()
