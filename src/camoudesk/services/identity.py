"""IdentityService: the anonymity check (core/identity_check.py) for a profile."""

import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from camoudesk.core import identity_check
from camoudesk.core.launcher import resolve_network
from camoudesk.models.profile import FingerprintSummary, Profile
from camoudesk.services.errors import NotFound

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext


class IdentityService:
    REAL_IP_TTL = 300

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._real_ip_cache: Optional[tuple] = None

    def real_ip(self) -> str:
        """This Mac's own public IP (asked directly, never through a proxy), cached 5 minutes:
        the check must know it to tell whether a browser leaks it."""
        cached = self._real_ip_cache
        if cached and time.time() - cached[1] < self.REAL_IP_TTL:
            return cached[0]
        import requests

        try:
            ip = str(requests.get("https://ipinfo.io/json", timeout=8, headers={"User-Agent": "CamouDesk"}).json().get("ip") or "")
        except Exception:
            ip = ""
        self._real_ip_cache = (ip, time.time())
        return ip

    def expected(self, profile: Profile) -> Dict[str, Any]:
        engine = self.ctx.engine
        saved = engine.load_identity(self.ctx.store.profile_dir(profile.id)) or {}
        summary = engine.summarize(profile.os, saved) if saved else FingerprintSummary(os=profile.os)
        net = resolve_network(profile, self.ctx.proxies)
        webgl = profile.fp_overrides.get("webgl")
        return {
            "os": profile.os, "user_agent": summary.user_agent, "screen": summary.screen, "cores": summary.cores,
            "gpu": webgl[1] if webgl else summary.gpu,
            "canvas_real": engine.canvas_policy(self.ctx.app_dir, profile, saved) is not True,
            "proxy": net.proxy.is_configured, "real_ip": self.real_ip(), "country": net.country_code,
            "timezone": net.timezone, "locale": net.locale, "block_webgl": bool(profile.fp_overrides.get("block_webgl")),
        }

    def check(self, profile_id: str) -> Dict[str, Any]:
        """Open the profile headless through its own network and grade what it exposes."""
        raw = self.ctx.manager.check_identity(profile_id)
        profile = self.ctx.store.get_profile(profile_id)
        if profile is None:
            raise NotFound("Profile not found")
        expected = identity_check.expected_from_claim(self.expected(profile), raw.get("claimed") or {})
        report = identity_check.evaluate(raw, expected)
        identity_check.save_report(self.ctx.store.profile_dir(profile.id), report)
        return report

    def report(self, profile_id: str) -> Optional[Dict[str, Any]]:
        if self.ctx.store.get_profile(profile_id) is None:
            raise NotFound("Profile not found")
        return identity_check.load_report(self.ctx.store.profile_dir(profile_id))
