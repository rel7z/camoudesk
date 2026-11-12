"""Proxy parsing and connectivity/geolocation health verification."""

import time
from typing import Any, Callable, Dict, Optional

import requests

from camoudesk.models.profile import ProxyConfig

DEFAULT_PROXY_PROBE_URL = "http://ip-api.com/json/?fields=status,country,city,timezone,query"


def check_proxy_health(
    raw_proxy_url: str,
    timeout: float = 8.0,
    probe_url: str = DEFAULT_PROXY_PROBE_URL,
    http_get: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Validate proxy syntax and measure exit IP, geo metadata, and round-trip latency.
    Supports http://, https://, socks5://, and socks5h:// URLs.
    """
    try:
        proxy_cfg = ProxyConfig.from_url(raw_proxy_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not proxy_cfg.is_configured:
        return {"ok": False, "error": "Proxy URL is empty"}

    requester = http_get or requests.get
    proxies = {
        "http": proxy_cfg.raw_url,
        "https": proxy_cfg.raw_url,
    }

    start = time.monotonic()
    try:
        resp = requester(
            probe_url,
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "CamouDesk-ProxyCheck/1.0"},
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        if getattr(resp, "status_code", 200) >= 400:
            return {"ok": False, "error": f"Proxy probe returned HTTP {resp.status_code}"}
        payload = resp.json() if hasattr(resp, "json") else {}
        ip = payload.get("query") or payload.get("origin") or "Unknown"
        return {
            "ok": True,
            "ip": str(ip),
            "country": str(payload.get("country") or ""),
            "city": str(payload.get("city") or ""),
            "timezone": str(payload.get("timezone") or ""),
            "latency_ms": latency_ms,
            "normalized_url": proxy_cfg.raw_url,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
