"""ProfileService: profiles, trash, folders/tags, fingerprints, previews and provider sessions."""

import os
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from camoudesk.core import geoip_db
from camoudesk.models.profile import Profile, parse_screen, validate_os_target
from camoudesk.services.errors import Conflict, Invalid, NotFound
from camoudesk.services.views import ProxyLookup, inline_proxy_url, profile_row

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext

# Fields that can change while the profile's browser is running (they never reach the browser).
RUNNING_EDITABLE = frozenset({"name", "notes", "folder", "tags"})


class ProfileService:
    PREVIEW_TTL = 30 * 60
    PREVIEW_MAX = 20

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        # Fingerprints shown in the New profile form, by one-time token: {token: (os, fp, created)}.
        self.previews: Dict[str, Any] = {}
        self._previews_lock = threading.Lock()

    # ---------------------------------------------------------------- reading

    def _get(self, profile_id: str) -> Profile:
        profile = self.ctx.store.get_profile(profile_id)
        if profile is None:
            raise NotFound("Profile not found")
        return profile

    def row(self, profile: Profile, lookup: Any = None) -> Dict[str, Any]:
        return profile_row(self.ctx, profile, lookup)

    def rows(self, trashed: bool = False) -> List[Dict[str, Any]]:
        lookup = ProxyLookup(self.ctx.proxies.list())
        return [self.row(p, lookup) for p in self.ctx.store.list_profiles() if bool(p.deleted_at) == trashed]

    def get(self, profile_id: str) -> Dict[str, Any]:
        return self.row(self._get(profile_id))

    # ---------------------------------------------------------------- trash

    def trash(self, profile_ids: List[str]) -> Dict[str, Any]:
        """Move profiles to the trash; their data stays on disk until purged. Running ones are stopped."""
        exited = self.ctx.manager.stop_many(profile_ids or [])  # in parallel, not one by one
        moved = 0
        for pid, ok in exited.items():
            if ok:
                self.ctx.store.set_trashed(pid, True)
                moved += 1
        return {"trashed": moved}

    def restore(self, profile_ids: List[str]) -> Dict[str, Any]:
        for pid in profile_ids or []:
            self.ctx.store.set_trashed(pid, False)
        return {"restored": len(profile_ids or [])}

    def purge(self, profile_ids: List[str]) -> Dict[str, Any]:
        """Delete forever (data wiped). Only profiles already in the trash can be purged."""
        store = self.ctx.store
        trashed = [pid for pid in profile_ids or [] if (p := store.get_profile(pid)) is not None and p.deleted_at]
        exited = self.ctx.manager.stop_many(trashed)
        purged = sum(1 for pid in trashed if exited.get(pid) and store.delete_profile(pid, wipe_data=True))
        return {"purged": purged}

    def empty_trash(self) -> Dict[str, Any]:
        return self.purge([p.id for p in self.ctx.store.list_profiles() if p.deleted_at])

    def delete(self, profile_id: str) -> Dict[str, Any]:
        stopped = self.ctx.manager.stop(profile_id)
        if not stopped.get("exited", True):
            raise Conflict("The browser is still running; profile data was not deleted.")
        if not self.ctx.store.delete_profile(profile_id, wipe_data=True):
            raise NotFound("Profile not found")
        return {"id": profile_id}

    # ---------------------------------------------------------------- organising

    def clone(self, profile_id: str, copy_data: bool = False, name: str = "") -> Dict[str, Any]:
        if copy_data and self.ctx.manager.is_running(profile_id):
            raise Invalid("Stop the profile before duplicating its data.")
        return self.row(self.ctx.store.clone_profile(profile_id, name=name, copy_data=bool(copy_data)))

    def bulk_meta(self, profile_ids: List[str], patch: Dict[str, Any]) -> Dict[str, Any]:
        """Folder/tag changes for many profiles: {folder?, add_tags?, remove_tags?}."""
        patch = patch or {}
        changed = self.ctx.store.bulk_update_metadata(
            profile_ids or [],
            folder=patch.get("folder") if "folder" in patch else None,
            add_tags=patch.get("add_tags"),
            remove_tags=patch.get("remove_tags"),
        )
        return {"changed": changed}

    def rename_folder(self, old: str, new: str) -> Dict[str, Any]:
        return {"changed": self.ctx.store.rename_folder(old, new)}

    # ---------------------------------------------------------------- create / update

    def normalize(self, payload: Dict[str, Any], current: Optional[Profile] = None) -> Dict[str, Any]:
        """Turn the form's proxy_mode/proxy_parts into stored fields."""
        data = dict(payload or {})
        mode = data.pop("proxy_mode", None)
        parts = data.pop("proxy_parts", None)
        if mode == "none":
            data["proxy"], data["proxy_id"] = "", ""
        elif mode == "saved":
            proxy_id = str(data.get("proxy_id") or "")
            if not self.ctx.proxies.get(proxy_id):
                raise Invalid("Choose a saved proxy")
            data["proxy"] = ""
        elif mode == "custom":
            data["proxy"] = inline_proxy_url(parts or {}, current)
            data["proxy_id"] = ""
        return data

    def _check_gpu(self, data: Dict[str, Any], os_key: str) -> None:
        webgl = (data.get("fp_overrides") or {}).get("webgl")
        if webgl and list(webgl) not in self.ctx.engine.gpu_options(os_key):
            raise Invalid("That graphics card is not available for this operating system.")

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload or {})
        token = payload.pop("fp_preview", None)
        defaults = self.ctx.settings.get()["new_profile"]
        data = {**defaults, **self.normalize(payload)}
        os_key = validate_os_target(data.get("os") or "macos")
        self._check_gpu(data, os_key)
        fingerprint = self.take_preview(token, os_key, (data.get("fp_overrides") or {}).get("screen"))
        return self.row(self.ctx.store.create_profile(data, fingerprint=fingerprint))

    def update(self, profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.ctx.manager.is_running(profile_id):
            # Labels don't affect the running browser; network, fingerprint and system settings do.
            locked = sorted(set(payload or {}) - RUNNING_EDITABLE)
            if locked:
                raise Invalid(
                    "Stop the running browser to change network, fingerprint or system settings "
                    f"({', '.join(locked)}). Name, notes, folder and tags can be edited while it runs."
                )
        current = self._get(profile_id)
        data = self.normalize(payload or {}, current)
        self._check_gpu(data, validate_os_target(data.get("os") or current.os))
        return self.row(self.ctx.store.update_profile(profile_id, data))

    # ---------------------------------------------------------------- fingerprints

    def regenerate_fingerprint(self, profile_id: str, screen: Optional[str] = None, source: Optional[str] = None) -> Dict[str, Any]:
        """New fingerprint; ``screen`` "WxH" pins the size, "" allows any, None keeps the current pin.
        ``source`` "generated" / "preset" (recorded real device); None keeps the current source."""
        if self.ctx.manager.is_running(profile_id):
            raise Invalid("Stop the running browser before regenerating its fingerprint.")
        kwargs: Dict[str, Any] = {"source": source} if source else {}
        if screen is not None:
            kwargs["screen"] = screen
        return self.row(self.ctx.store.regenerate_fingerprint(profile_id, **kwargs))

    def fingerprint_options(self, profile_os: str) -> Dict[str, Any]:
        os_key = validate_os_target(profile_os)
        return {
            "gpus": self.ctx.engine.gpu_options(os_key),
            "screens": self.ctx.engine.screen_presets(os_key),
            "geoip_installed": bool(geoip_db.status().get("installed")),
        }

    def preview_fingerprint(self, profile_os: str, screen: Optional[str] = None, source: str = "generated",
                            match_host: bool = False) -> Dict[str, Any]:
        """A new device for the New profile form: its summary, and a one-time token that makes
        ``create`` use exactly this fingerprint. The fingerprint itself stays here.

        ``source``: "generated" (fpgen, unique) or "preset" (a recorded real device).
        ``match_host``: a device this Mac can show the real canvas for (its OS; for presets its GPU).
        """
        from camoudesk.core.fingerprint import summary_dict

        engine = self.ctx.engine
        os_key = validate_os_target(engine.host_os if match_host else profile_os)  # type: ignore[attr-defined]
        pinned = parse_screen(screen) if screen else None
        if screen and not pinned:
            raise Invalid(f"Invalid screen size '{screen}'")
        if source not in ("generated", "preset"):
            raise Invalid("Fingerprint source must be generated or preset")
        gpu = engine.host_gpu(self.ctx.app_dir) if match_host else None  # type: ignore[attr-defined]
        fp = engine.generate_identity(os_key, pinned, source, gpu_renderer=gpu[1] if gpu and source == "preset" else None)
        token = os.urandom(12).hex()
        now = time.time()
        with self._previews_lock:
            live = {k: v for k, v in self.previews.items() if now - v[2] < self.PREVIEW_TTL}
            while len(live) >= self.PREVIEW_MAX:  # keep the newest ones
                live.pop(min(live, key=lambda k: live[k][2]))
            live[token] = (os_key, fp, now)
            self.previews.clear()
            self.previews.update(live)
        canvas = engine.canvas_policy(self.ctx.app_dir, Profile(id="preview", name="preview", os=os_key), fp)
        return {"token": token, "os": os_key, "summary": summary_dict(engine.summarize(os_key, fp)), "canvas_real": canvas is not True}

    def take_preview(self, token: Any, os_key: str, screen: Any) -> Optional[Dict[str, Any]]:
        """The previewed fingerprint for ``token`` if it fits this profile (one use only)."""
        if not isinstance(token, str) or not token:
            return None
        with self._previews_lock:
            entry = self.previews.pop(token, None)
        if entry is None:
            return None
        p_os, fp, created = entry
        if p_os != os_key or time.time() - created > self.PREVIEW_TTL:
            return None
        pinned = parse_screen(screen) if screen else None
        scr = fp.get("screen") or {}
        if pinned and (scr.get("width"), scr.get("height")) != tuple(pinned):
            return None
        return fp

    # ---------------------------------------------------------------- proxies on profiles

    def assign_proxy(self, profile_ids: List[str], proxy_id: str) -> Dict[str, Any]:
        """Bulk: point profiles at a saved proxy ("" = Direct). Running profiles are skipped."""
        if proxy_id and not self.ctx.proxies.get(proxy_id):
            raise NotFound("Saved proxy not found")
        changed, skipped = 0, 0
        for pid in profile_ids or []:
            if self.ctx.manager.is_running(pid):
                skipped += 1
                continue
            self.ctx.store.update_profile(pid, {"proxy_id": proxy_id, "proxy": ""} if proxy_id else {"proxy_id": "", "proxy": ""})
            changed += 1
        return {"changed": changed, "skipped_running": skipped}

    def new_ip(self, profile_ids: List[str]) -> Dict[str, Any]:
        """New provider session (new IP) for stopped profiles; running ones are skipped."""
        ids = [pid for pid in (profile_ids or []) if not self.ctx.manager.is_running(pid)]
        lookup = ProxyLookup(self.ctx.proxies.list())
        eligible = [pid for pid in ids
                    if (prof := self.ctx.store.get_profile(pid)) is not None and prof.proxy_id
                    and (sp := lookup.get(prof.proxy_id)) is not None and sp.provider]
        changed = self.ctx.store.new_provider_session(eligible)
        return {"changed": len(changed), "skipped_running": len(profile_ids or []) - len(ids),
                "skipped_no_provider": len(ids) - len(eligible)}
