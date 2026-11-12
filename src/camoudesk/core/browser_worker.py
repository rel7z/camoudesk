"""Standalone Camoufox persistent browser worker executed per active profile."""

import argparse
import signal
import sys
import threading
from typing import Any, Dict, Optional

from camoufox.sync_api import Camoufox

from camoudesk.core.fingerprint import ensure_profile_fingerprint
from camoudesk.core.storage import ProfileStore
from camoudesk.models.profile import ProxyConfig


def build_camoufox_kwargs(
    profile_os: str,
    user_data_dir: str,
    saved_fingerprint: Dict[str, Any],
    proxy_url: str = "",
    timezone: str = "",
    locale: str = "en-US",
    block_webrtc: bool = True,
    humanize: bool = True,
    canvas_noise: bool = True,
    headless: bool = False,
) -> Dict[str, Any]:
    """Build verified keyword arguments for Camoufox(persistent_context=True, ...)."""
    config: Dict[str, Any] = {}
    if timezone:
        config["timezone"] = timezone

    kwargs: Dict[str, Any] = {
        "persistent_context": True,
        "user_data_dir": user_data_dir,
        "fingerprint": saved_fingerprint,
        "i_know_what_im_doing": True,
        "os": profile_os,
        "locale": locale or "en-US",
        "block_webrtc": block_webrtc,
        "humanize": humanize,
        # Avoid Camoufox nested host_gpu() subprocess probe which can deadlock Juggler pipes on macOS.
        "canvas_noise": canvas_noise,
        "headless": headless,
    }
    if config:
        kwargs["config"] = config

    proxy_cfg = ProxyConfig.from_url(proxy_url).to_playwright_proxy() if proxy_url else None
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg

    return kwargs


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

    pdir = store.profile_dir(profile.id)
    ff_dir = store.firefox_data_dir(profile.id)
    ff_dir.mkdir(parents=True, exist_ok=True)
    saved_fp, _ = ensure_profile_fingerprint(pdir, profile.os)
    store.mark_used(profile.id)

    stop_event = threading.Event()
    closed_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        print(f"[INFO] Signal {signum} received; shutting down browser cleanly.", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    target_url = (url_override or profile.startup_url or "").strip()
    kwargs = build_camoufox_kwargs(
        profile_os=profile.os,
        user_data_dir=str(ff_dir),
        saved_fingerprint=saved_fp,
        proxy_url=profile.proxy,
        timezone=profile.timezone,
        locale=profile.locale,
        block_webrtc=profile.block_webrtc,
        humanize=profile.humanize,
        headless=headless,
    )

    print(
        f"[INFO] Launching Camoufox profile={profile.id} os={profile.os} headless={headless}",
        flush=True,
    )

    try:
        with Camoufox(**kwargs) as context:
            context.on("close", lambda *_: closed_event.set())
            pages = list(context.pages)
            page = pages[0] if pages else context.new_page()
            if target_url:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="CamouDesk Persistent Browser Worker")
    parser.add_argument("--profile-id", required=True, help="Target profile ID")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--url", help="Override startup URL")
    parser.add_argument("--max-seconds", type=float, default=None, help="Optional test lifetime limit")
    args = parser.parse_args()
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
