"""Proxy parsing and connectivity/geolocation health verification."""

import time
from typing import Any, Callable, Dict, Optional

import requests

from camoudesk.models.profile import ProxyConfig

DEFAULT_PROXY_PROBE_URL = "http://ip-api.com/json/?fields=status,country,countryCode,city,timezone,query"


def check_proxy_config(cfg: ProxyConfig, **kwargs: Any) -> Dict[str, Any]:
    """check_proxy_health for an already-parsed proxy (saved proxies, resolved profile networks)."""
    return check_proxy_health(cfg.raw_url, **kwargs)


# Exit-IP probes, tried in order. ipinfo.io is reached over HTTPS, so a malicious proxy cannot
# forge the answer; ip-api.com (HTTP only, 45 requests/minute) is the fallback when the first
# service rate-limits or errors. Both report IP, country code, city and timezone.
HTTPS_PROBE_URL = "https://ipinfo.io/json"
PROBES = (HTTPS_PROBE_URL, DEFAULT_PROXY_PROBE_URL)


def _scrub(text: str) -> str:
    """Remove any user:password@ from technical error text."""
    import re

    return re.sub(r"://[^/@\s]+@", "://***@", text)


def friendly_error(exc: BaseException) -> str:
    """A short, actionable message for a failure to get through the proxy (no credentials)."""
    text = str(exc)
    low = text.lower()
    if "407" in text or "proxy authentication required" in low:
        return ("The proxy rejected the username or password (HTTP 407). "
                "Re-paste the proxy exactly as your provider gave it.")
    if "socks" in low and ("auth" in low or "credentials" in low or "0x01" in low):
        return "The SOCKS proxy rejected the username or password. Re-paste the proxy exactly as your provider gave it."
    if isinstance(exc, requests.Timeout) or "timed out" in low or "timeout" in low:
        return "The proxy did not answer in time. It may be offline or blocking this connection."
    if "refused" in low or "errno 61" in low:
        return "The proxy refused the connection. Check the host and port."
    if "name or service not known" in low or "nodename nor servname" in low or "failed to resolve" in low or "getaddrinfo" in low:
        return "The proxy host could not be found. Check the host name."
    if "tunnel connection failed" in low:
        code = next((c for c in ("400", "403", "404", "429", "500", "502", "503") if c in text), "")
        return f"The proxy refused to open the connection{f' (HTTP {code})' if code else ''}."
    return f"Could not connect through the proxy ({type(exc).__name__})."


class _ProbeError(Exception):
    """The probe service failed (rate limit, bad answer); another service may still work."""


def _parse_probe(payload: Any) -> Dict[str, str]:
    if not isinstance(payload, dict):
        raise _ProbeError("Exit IP lookup returned an unexpected answer")
    if payload.get("status") == "fail" or payload.get("bogon"):
        raise _ProbeError(f"Exit IP lookup failed: {payload.get('message') or 'unknown error'}")
    ip = payload.get("query") or payload.get("ip") or payload.get("origin")
    if not ip:
        raise _ProbeError("Exit IP lookup returned no IP address")
    # ip-api: country = name, countryCode = ISO code; ipinfo: country = ISO code only.
    country = str(payload.get("country") or "")
    code = str(payload.get("countryCode") or (country if len(country) == 2 else ""))
    return {
        "ip": str(ip),
        "country": "" if country == code and "countryCode" not in payload else country,
        "country_code": code.upper(),
        "city": str(payload.get("city") or ""),
        "timezone": str(payload.get("timezone") or ""),
    }


def check_proxy_health(
    raw_proxy_url: str,
    timeout: float = 8.0,
    probe_url: Optional[str] = None,
    http_get: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Validate proxy syntax and measure exit IP, geo metadata, and round-trip latency.
    Supports http://, https://, socks4://, socks5:// and socks5h:// URLs.
    """
    try:
        proxy_cfg = ProxyConfig.from_url(raw_proxy_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not proxy_cfg.is_configured:
        return {"ok": False, "error": "Proxy URL is empty"}

    requester = http_get or requests.get
    proxies = {"http": proxy_cfg.raw_url, "https": proxy_cfg.raw_url}
    last_error = "Exit IP lookup failed"
    for url in (probe_url,) if probe_url else PROBES:
        start = time.monotonic()
        try:
            resp = requester(url, proxies=proxies, timeout=timeout, headers={"User-Agent": "CamouDesk-ProxyCheck/1.0"})
        except Exception as exc:
            # Could not get through the proxy at all: another probe service will not help.
            return {"ok": False, "error": friendly_error(exc), "detail": _scrub(str(exc))[:300]}
        latency_ms = int((time.monotonic() - start) * 1000)
        status = getattr(resp, "status_code", 200)
        try:
            if status >= 400:
                raise _ProbeError(f"Proxy probe returned HTTP {status}" + (" (rate limited)" if status == 429 else ""))
            info = _parse_probe(resp.json() if hasattr(resp, "json") else {})
        except _ProbeError as exc:
            last_error = str(exc)
            continue
        except ValueError:
            last_error = "Exit IP lookup returned invalid JSON"
            continue
        return {"ok": True, **info, "latency_ms": latency_ms}
    return {"ok": False, "error": last_error}
