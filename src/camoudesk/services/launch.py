"""LaunchService: start/stop browsers, logs, diagnostics and the public test sites."""

from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext

TEST_SITES = (
    "https://pixelscan.net",
    "https://browserleaks.com/ip",
    "https://browserleaks.com/webrtc",
    "https://browserleaks.com/canvas",
    "https://abrahamjuliot.github.io/creepjs/",
)


class LaunchService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    def launch(self, profile_id: str, headless: bool = False) -> Dict[str, Any]:
        return self.ctx.manager.launch(profile_id, headless=headless)

    def stop(self, profile_id: str) -> Dict[str, Any]:
        """Starts a graceful stop and returns at once; listeners are notified when it finishes."""
        return self.ctx.manager.stop_async(profile_id)

    def status(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.manager.get_status(profile_id)

    def logs(self, profile_id: str, max_lines: int = 300) -> str:
        return self.ctx.manager.read_logs(profile_id, max_lines=max(1, min(int(max_lines), 2000)))

    def diagnostics(self, profile_id: str) -> str:
        return self.ctx.manager.diagnostics(profile_id)

    def open_test_sites(self, profile_id: str) -> Dict[str, Any]:
        """Start the profile with pixelscan, BrowserLeaks and CreepJS open in tabs."""
        return self.ctx.manager.launch(profile_id, url_override=list(TEST_SITES))
