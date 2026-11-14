"""SettingsService: app settings; publishes ``settings`` (and ``theme`` when it changes)."""

from typing import TYPE_CHECKING, Any, Dict

from camoudesk.core import geoip_db

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext


class SettingsService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    def get(self) -> Dict[str, Any]:
        return self.ctx.settings.get()

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        updated = self.ctx.settings.update(patch or {})
        self.ctx.events.publish("settings", settings=updated)
        if "theme" in (patch or {}):
            self.ctx.events.publish("theme", theme=updated["theme"])
        return updated

    def rotate_api_token(self) -> Dict[str, Any]:
        return {"token": self.ctx.settings.rotate_api_token()}

    @staticmethod
    def geoip_download() -> Dict[str, Any]:
        return geoip_db.download()

    @staticmethod
    def geoip_status() -> Dict[str, Any]:
        return geoip_db.status()
