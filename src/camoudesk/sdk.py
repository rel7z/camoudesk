"""Python SDK: drive a saved CamouDesk profile with Playwright from your own script.

    from camoudesk.sdk import open_profile

    with open_profile("prof_1794470000_ab12") as context:   # or headless=True
        page = context.new_page()
        page.goto("https://example.com")

The context uses the profile's saved fingerprint, proxy (credentials included), timezone/locale
and fingerprint overrides, and its persistent data (cookies, storage), exactly like a launch
from the app. While the script runs, the profile shows as running in CamouDesk; stopping it
there sends SIGTERM to this script. A profile already open elsewhere is refused.
"""

from contextlib import contextmanager
import json
import os
import sys
from typing import Any, Iterator, List, Optional

from camoudesk.core.launcher import ensure_location, profile_camoufox_kwargs, resolve_network
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.storage import ProfileStore
from camoudesk.models.profile import Profile

__all__ = ["open_profile", "list_profiles", "ProfileBusyError"]


class ProfileBusyError(RuntimeError):
    """The profile is already running (in the app, a worker, or another script)."""


def list_profiles(store: Optional[ProfileStore] = None) -> List[Profile]:
    """Profiles not in the trash."""
    return [p for p in (store or ProfileStore()).list_profiles() if not p.deleted_at]


def _running_elsewhere(store: ProfileStore, profile_id: str) -> bool:
    from camoudesk.manager.process_manager import _pid_alive, process_start_time

    path = store.run_file(profile_id)
    if not path.is_file():
        return False
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        pid, started = int(rec["pid"]), str(rec.get("started") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return _pid_alive(pid) and bool(started) and process_start_time(pid) == started


@contextmanager
def open_profile(
    profile_id: str,
    headless: bool = False,
    store: Optional[ProfileStore] = None,
    **camoufox_overrides: Any,
) -> Iterator[Any]:
    """Open a profile as a Playwright persistent ``BrowserContext`` (see module docstring).

    ``camoufox_overrides`` are passed to ``camoufox.sync_api.Camoufox`` on top of the
    profile's settings (e.g. ``block_images=True``); identity keys are not meant to be changed.
    """
    from camoufox.sync_api import Camoufox

    from camoudesk.manager.process_manager import MODE_SDK, process_start_time

    store = store or ProfileStore()
    profile = store.get_profile(profile_id)
    if profile is None:
        raise KeyError(f"Profile '{profile_id}' not found")
    if profile.deleted_at:
        raise ProfileBusyError("This profile is in the trash; restore it first.")
    if _running_elsewhere(store, profile.id):
        raise ProfileBusyError("This profile is already running; stop it first.")
    proxies = ProxyStore(store.app_dir)
    network = resolve_network(profile, proxies)
    if network.missing_saved_proxy:
        raise RuntimeError("This profile's saved proxy was deleted; choose another proxy or Direct.")
    # Same pre-launch location check as the app, so "auto" timezone never falls back to this Mac's.
    network = ensure_location(profile, network, store, proxies, lambda msg: print(f"[CamouDesk] {msg}", file=sys.stderr))
    profile = store.get_profile(profile.id) or profile
    kwargs, _ = profile_camoufox_kwargs(profile, store, network, headless=headless)
    kwargs.update(camoufox_overrides)

    run_file = store.run_file(profile.id)
    pid = os.getpid()
    record = {"pid": pid, "pgid": pid, "mode": MODE_SDK, "started": process_start_time(pid)}
    run_file.write_text(json.dumps(record), encoding="utf-8")
    try:
        store.mark_used(profile.id)
        with Camoufox(**kwargs) as context:
            yield context
    finally:
        try:
            current = json.loads(run_file.read_text(encoding="utf-8"))
            if current.get("pid") == pid:
                run_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
