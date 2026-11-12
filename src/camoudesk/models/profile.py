"""Data models and validation helpers for browser profiles and proxies."""

from dataclasses import asdict, dataclass, field
import ipaddress
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from camoudesk.config import DEFAULT_STARTUP_URL, SUPPORTED_OS

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_ALLOWED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def sanitize_profile_id(profile_id: str) -> str:
    """Validate profile ID to prevent path traversal or shell injection."""
    if not isinstance(profile_id, str) or not _SAFE_ID_RE.fullmatch(profile_id.strip()):
        raise ValueError("Profile ID must be 1-64 alphanumeric characters, underscores, or hyphens")
    return profile_id.strip()


def normalize_ip(value: Any) -> str:
    """Return a canonical IPv4/IPv6 string, or "" when value is empty or not an IP address."""
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def validate_os_target(os_target: str) -> str:
    """Validate target OS string against supported Camoufox platforms."""
    normalized = (os_target or "macos").strip().lower()
    if normalized not in SUPPORTED_OS:
        raise ValueError(f"Unsupported OS '{os_target}'. Must be one of: {', '.join(SUPPORTED_OS)}")
    return normalized


@dataclass
class ProxyConfig:
    """Normalized proxy representation supporting HTTP, HTTPS, and SOCKS5."""

    raw_url: str = ""
    scheme: str = ""
    host: str = ""
    port: Optional[int] = None
    username: str = ""
    password: str = ""

    @classmethod
    def from_url(cls, raw_url: str) -> "ProxyConfig":
        """Parse and validate a proxy URL string. Empty input returns an empty direct config."""
        cleaned = (raw_url or "").strip()
        if not cleaned:
            return cls()

        if "://" not in cleaned:
            cleaned = f"http://{cleaned}"

        parsed = urlsplit(cleaned)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_PROXY_SCHEMES:
            raise ValueError(f"Unsupported proxy scheme '{scheme}'. Use http, https, or socks5.")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("Proxy URL must include both host and port (e.g. http://host:port)")

        canonical = urlunsplit((scheme, parsed.netloc, "", "", ""))
        return cls(
            raw_url=canonical,
            scheme=scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username or "",
            password=parsed.password or "",
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.port)

    @property
    def has_credentials(self) -> bool:
        return bool(self.username or self.password)

    def to_firefox_prefs(self) -> Dict[str, Any]:
        """Firefox prefs that route all traffic through this (credential-less) proxy.

        Firefox cannot answer SOCKS5 auth and would show an interactive prompt for HTTP
        proxy auth, so credentialed proxies must go through the Playwright worker instead.
        """
        if not self.is_configured:
            return {}
        if self.has_credentials:
            raise ValueError("Firefox prefs cannot carry proxy credentials")
        prefs: Dict[str, Any] = {
            # Never silently fall back to a direct connection (would leak the real IP).
            "network.proxy.failover_direct": False,
            "network.proxy.no_proxies_on": "",
        }
        if self.scheme in ("socks5", "socks5h"):
            prefs.update(
                {
                    "network.proxy.type": 1,
                    "network.proxy.socks": self.host,
                    "network.proxy.socks_port": int(self.port or 0),
                    "network.proxy.socks_version": 5,
                    # Resolve DNS through the proxy for both socks5 and socks5h to avoid DNS leaks.
                    "network.proxy.socks_remote_dns": True,
                }
            )
        elif self.scheme == "https":
            # Manual prefs cannot express a TLS connection to the proxy; a PAC "HTTPS" entry can.
            pac = f"function FindProxyForURL(u,h){{return 'HTTPS {self.host}:{self.port}';}}"
            prefs.update(
                {
                    "network.proxy.type": 2,
                    "network.proxy.autoconfig_url": "data:application/x-ns-proxy-autoconfig," + pac,
                }
            )
        else:
            prefs.update(
                {
                    "network.proxy.type": 1,
                    "network.proxy.http": self.host,
                    "network.proxy.http_port": int(self.port or 0),
                    "network.proxy.ssl": self.host,
                    "network.proxy.ssl_port": int(self.port or 0),
                    "network.proxy.share_proxy_settings": True,
                }
            )
        return prefs

    def to_playwright_proxy(self) -> Optional[Dict[str, str]]:
        """Build Playwright/Camoufox proxy dict with separate credentials when present."""
        if not self.is_configured:
            return None
        server = f"{self.scheme}://{self.host}:{self.port}"
        cfg: Dict[str, str] = {"server": server}
        if self.username:
            cfg["username"] = self.username
        if self.password:
            cfg["password"] = self.password
        return cfg

    def masked_display(self) -> str:
        """Return proxy label safe for UI badges without exposing plaintext password."""
        if not self.is_configured:
            return "Direct (No Proxy)"
        if self.username:
            return f"{self.scheme}://{self.username}:***@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass
class FingerprintSummary:
    """Human-readable hardware summary derived from a frozen fpgen fingerprint."""

    os: str = "macos"
    user_agent: str = ""
    screen: str = ""
    cores: Optional[int] = None
    gpu: str = ""

    @classmethod
    def from_fpgen_dict(cls, os_target: str, fp: Dict[str, Any]) -> "FingerprintSummary":
        if not isinstance(fp, dict):
            return cls(os=os_target)
        nav = fp.get("navigator") or {}
        scr = fp.get("screen") or {}
        gpu_obj = fp.get("gpu") or {}
        webgl_obj = fp.get("webgl") or {}
        width = scr.get("width")
        height = scr.get("height")
        screen_str = f"{width}x{height}" if width and height else ""
        gpu_str = (
            gpu_obj.get("renderer")
            or webgl_obj.get("unmaskedRenderer")
            or webgl_obj.get("renderer")
            or ""
        )
        return cls(
            os=os_target,
            user_agent=str(nav.get("userAgent") or ""),
            screen=screen_str,
            cores=nav.get("hardwareConcurrency"),
            gpu=str(gpu_str),
        )


@dataclass
class Profile:
    """Persistent anti-detect browser profile metadata."""

    id: str
    name: str
    os: str = "macos"
    proxy: str = ""
    proxy_exit_ip: str = ""
    timezone: str = "America/New_York"
    locale: str = "en-US"
    startup_url: str = DEFAULT_STARTUP_URL
    block_webrtc: bool = True
    humanize: bool = True
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    fingerprint_summary: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    last_used_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Profile":
        """Rehydrate Profile from persisted dictionary with safe backward-compatible defaults."""
        profile_id = sanitize_profile_id(str(raw["id"]))
        os_target = validate_os_target(str(raw.get("os") or "macos"))
        proxy_raw = str(raw.get("proxy") or "").strip()
        if proxy_raw:
            proxy_raw = ProxyConfig.from_url(proxy_raw).raw_url
        return cls(
            id=profile_id,
            name=str(raw.get("name") or profile_id).strip() or profile_id,
            os=os_target,
            proxy=proxy_raw,
            proxy_exit_ip=normalize_ip(raw.get("proxy_exit_ip")) if proxy_raw else "",
            timezone=str(raw.get("timezone") or "America/New_York").strip(),
            locale=str(raw.get("locale") or "en-US").strip(),
            startup_url=str(raw.get("startup_url") or raw.get("target_url") or DEFAULT_STARTUP_URL).strip(),
            block_webrtc=bool(raw.get("block_webrtc", True)),
            humanize=bool(raw.get("humanize", True)),
            notes=str(raw.get("notes") or "").strip(),
            tags=[str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
            fingerprint_summary=dict(raw.get("fingerprint_summary") or {}),
            created_at=str(raw.get("created_at") or time.strftime("%Y-%m-%d %H:%M")),
            last_used_at=raw.get("last_used_at"),
        )
