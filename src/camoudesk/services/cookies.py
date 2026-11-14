"""CookieService: read, export, import and clear a profile's cookies."""

import os
from typing import TYPE_CHECKING, Any, Dict, List

from camoudesk.core import cookies as cookies_mod
from camoudesk.services.errors import Invalid, NotFound

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext

MAX_COOKIE_FILE = 5 * 1024 * 1024


class CookieService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    def _cookies(self, profile_id: str) -> List[Dict[str, Any]]:
        if self.ctx.store.get_profile(profile_id) is None:
            raise NotFound("Profile not found")
        return cookies_mod.read_profile_cookies(self.ctx.store.firefox_data_dir(profile_id))

    def summary(self, profile_id: str) -> Dict[str, Any]:
        total, sites = cookies_mod.summarize(self._cookies(profile_id))
        return {"total": total, "sites": sites}

    def export_text(self, profile_id: str, fmt: str = "json") -> str:
        cookies = self._cookies(profile_id)
        return cookies_mod.to_netscape(cookies) if fmt == "netscape" else cookies_mod.to_json(cookies)

    def default_filename(self, profile_id: str, fmt: str = "json") -> str:
        profile = self.ctx.store.get_profile(profile_id)
        if profile is None:
            raise NotFound("Profile not found")
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in profile.name)[:40] or profile.id
        return f"{safe_name}-cookies.{'txt' if fmt == 'netscape' else 'json'}"

    def save(self, profile_id: str, path: str, fmt: str = "json") -> Dict[str, Any]:
        cookies = self._cookies(profile_id)
        text = cookies_mod.to_netscape(cookies) if fmt == "netscape" else cookies_mod.to_json(cookies)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"saved": True, "path": path, "count": len(cookies)}

    def import_text(self, profile_id: str, text: str) -> Dict[str, Any]:
        if self.ctx.manager.is_running(profile_id):
            raise Invalid("Stop the profile before importing cookies.")
        cookies = cookies_mod.parse_cookies(str(text or ""))
        result = self.ctx.manager.import_cookies(profile_id, cookies)
        return {"imported": len(cookies), "total": result.get("after")}

    def import_file(self, profile_id: str, path: str) -> Dict[str, Any]:
        if os.path.getsize(path) > MAX_COOKIE_FILE:
            raise Invalid("Cookie file is larger than 5 MB")
        with open(path, encoding="utf-8", errors="replace") as fh:
            return self.import_text(profile_id, fh.read())

    def clear(self, profile_id: str) -> Dict[str, Any]:
        if self.ctx.manager.is_running(profile_id):
            raise Invalid("Stop the profile before clearing cookies.")
        self.ctx.manager.clear_cookies(profile_id)
        return {"cleared": True}
