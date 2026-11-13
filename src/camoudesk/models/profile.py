"""Data models and validation helpers for browser profiles and proxies."""

from dataclasses import asdict, dataclass, field
import ipaddress
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

from camoudesk.config import DEFAULT_STARTUP_URL, SUPPORTED_OS

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_ALLOWED_PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


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


def safe_ref(value: Any) -> str:
    """A reference to another record's id, or "" when absent/invalid (never raises)."""
    text = str(value or "").strip()
    return text if _SAFE_ID_RE.fullmatch(text) else ""


def clean_provider_opts(value: Any) -> Dict[str, Any]:
    """Per-profile provider settings: session rotation counter, targeting/mode overrides, and the
    key of the session its cached exit location belongs to."""
    from camoudesk.core.providers import evomi

    raw = value if isinstance(value, dict) else {}
    try:
        out: Dict[str, Any] = evomi.clean_opts(raw, partial=True)
    except ValueError:
        out = {}
    try:
        rotation = int(raw.get("rotation") or 0)
    except (TypeError, ValueError):
        rotation = 0
    if 0 < rotation < 10**9:
        out["rotation"] = rotation
    checked = str(raw.get("checked") or "")
    if re.fullmatch(r"[0-9a-f]{16}", checked):
        out["checked"] = checked
    return out


def normalize_country(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z]{2}", text) else ""


def clean_tags(value: Any) -> List[str]:
    seen: List[str] = []
    for t in value if isinstance(value, list) else []:
        tag = str(t).strip()[:40]
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:30]


_URL_SCHEMES = ("http", "https")


def clean_start_urls(value: Any, strict: bool = True) -> List[str]:
    """Normalise start URLs: http(s) or about:, bare domains get https://, max 20.

    ``javascript:``, ``data:``, ``file:`` and other schemes are rejected (strict) or dropped
    (when reading old registries), so a profile can never auto-run script or open local files.
    """
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        url = str(item or "").strip()
        if not url:
            continue
        # "javascript:x", "data:...", "mailto:" have a scheme but no "//"; "example.com:8080"
        # is a bare host with a port (digits right after the colon).
        scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)", url)
        if url.lower().startswith("about:"):
            pass
        elif scheme_match is None:
            url = "https://" + url
        else:
            scheme = scheme_match.group(1).lower()
            if scheme not in _URL_SCHEMES:
                if strict:
                    raise ValueError(f"Start URL must be http(s) or about: (got '{scheme}:')")
                continue
        if urlsplit(url).scheme in _URL_SCHEMES and not urlsplit(url).hostname:
            if strict:
                raise ValueError(f"Start URL has no host: {url}")
            continue
        if url not in out:
            out.append(url)
    return out[:20]


WEBRTC_MODES = ("block", "spoof", "real")
GEOLOCATION_MODES = ("auto", "prompt", "block")
_SCREEN_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")


def parse_screen(value: Any) -> Optional[Tuple[int, int]]:
    """"1920x1080" -> (1920, 1080); None for empty/invalid or implausible sizes."""
    m = _SCREEN_RE.match(str(value or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if 800 <= w <= 7680 and 600 <= h <= 4320 else None


def clean_fp_overrides(value: Any) -> Dict[str, Any]:
    """Keep only known fingerprint override keys with valid values (unknown keys are dropped).

    * webgl: [vendor, renderer] from Camoufox's recorded GPUs (validated per OS by the caller)
    * webrtc: block | spoof (report the proxy IP; needs GeoIP) | real
    * geolocation: auto (match proxy via GeoIP) | prompt | block
    * screen: "WxH" the fingerprint was generated for ("" = any)
    * block_images / block_webgl: bool
    """
    raw = value if isinstance(value, dict) else {}
    out: Dict[str, Any] = {}
    webgl = raw.get("webgl")
    if isinstance(webgl, (list, tuple)) and len(webgl) == 2 and all(isinstance(x, str) and x.strip() for x in webgl):
        out["webgl"] = [webgl[0].strip()[:120], webgl[1].strip()[:200]]
    if raw.get("webrtc") in WEBRTC_MODES:
        out["webrtc"] = raw["webrtc"]
    if raw.get("geolocation") in GEOLOCATION_MODES:
        out["geolocation"] = raw["geolocation"]
    screen = parse_screen(raw.get("screen"))
    if screen:
        out["screen"] = f"{screen[0]}x{screen[1]}"
    for key in ("block_images", "block_webgl"):
        if key in raw:
            out[key] = bool(raw[key])
    return out


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
            raise ValueError(f"Unsupported proxy scheme '{scheme}'. Use http, https, socks4, or socks5.")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("Proxy URL must include both host and port (e.g. http://host:port)")

        if scheme == "socks4" and (parsed.username or parsed.password):
            raise ValueError("SOCKS4 proxies cannot use a username/password; use SOCKS5 instead.")

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
        if self.scheme in ("socks4", "socks5", "socks5h"):
            prefs.update(
                {
                    "network.proxy.type": 1,
                    "network.proxy.socks": self.host,
                    "network.proxy.socks_port": int(self.port or 0),
                    "network.proxy.socks_version": 4 if self.scheme == "socks4" else 5,
                    # Resolve DNS through the proxy (SOCKS4a / SOCKS5h) to avoid DNS leaks.
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
        # URL userinfo is percent-encoded; Playwright wants the literal credentials.
        if self.username:
            cfg["username"] = unquote(self.username)
        if self.password:
            cfg["password"] = unquote(self.password)
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
    proxy: str = ""  # inline proxy URL; ignored when proxy_id names a saved proxy
    proxy_exit_ip: str = ""  # last check of the inline proxy
    proxy_timezone: str = ""
    proxy_country: str = ""
    proxy_id: str = ""  # saved proxy (ProxyStore) reference
    timezone: str = "auto"
    locale: str = "auto"
    startup_url: str = DEFAULT_STARTUP_URL  # first of start_urls; kept for older readers
    start_urls: list[str] = field(default_factory=lambda: [DEFAULT_STARTUP_URL])
    block_webrtc: bool = True
    humanize: bool = True
    notes: str = ""
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    fp_overrides: Dict[str, Any] = field(default_factory=dict)
    provider_opts: Dict[str, Any] = field(default_factory=dict)  # see clean_provider_opts
    fingerprint_summary: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    last_used_at: Optional[str] = None
    deleted_at: Optional[str] = None  # set while the profile is in the trash

    def to_dict(self) -> Dict[str, Any]:
        self.startup_url = self.start_urls[0] if self.start_urls else ""
        return asdict(self)

    @property
    def webrtc_mode(self) -> str:
        """block | spoof | real. Overrides win; otherwise the legacy block_webrtc flag decides."""
        return self.fp_overrides.get("webrtc") or ("block" if self.block_webrtc else "real")

    def set_start_urls(self, urls: Any) -> None:
        self.start_urls = clean_start_urls(urls)
        self.startup_url = self.start_urls[0] if self.start_urls else ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Profile":
        """Rehydrate Profile from persisted dictionary with safe backward-compatible defaults."""
        profile_id = sanitize_profile_id(str(raw["id"]))
        os_target = validate_os_target(str(raw.get("os") or "macos"))
        proxy_raw = str(raw.get("proxy") or "").strip()
        if proxy_raw:
            proxy_raw = ProxyConfig.from_url(proxy_raw).raw_url
        urls = raw.get("start_urls")
        if not isinstance(urls, list) or not urls:
            urls = [raw.get("startup_url") or raw.get("target_url") or DEFAULT_STARTUP_URL]
        start_urls = clean_start_urls(urls, strict=False)
        overrides = raw.get("fp_overrides")
        proxy_id = safe_ref(raw.get("proxy_id"))
        keep_exit = bool(proxy_raw or proxy_id)  # inline proxy, or a provider session's last check
        return cls(
            id=profile_id,
            name=str(raw.get("name") or profile_id).strip() or profile_id,
            os=os_target,
            proxy=proxy_raw,
            proxy_exit_ip=normalize_ip(raw.get("proxy_exit_ip")) if keep_exit else "",
            proxy_timezone=str(raw.get("proxy_timezone") or "").strip() if keep_exit else "",
            proxy_country=normalize_country(raw.get("proxy_country")) if keep_exit else "",
            proxy_id=proxy_id,
            timezone=str(raw.get("timezone") or "auto").strip(),
            locale=str(raw.get("locale") or "auto").strip(),
            startup_url=start_urls[0] if start_urls else "",
            start_urls=start_urls,
            block_webrtc=bool(raw.get("block_webrtc", True)),
            humanize=bool(raw.get("humanize", True)),
            notes=str(raw.get("notes") or "").strip(),
            folder=str(raw.get("folder") or "").strip()[:80],
            tags=clean_tags(raw.get("tags")),
            fp_overrides=clean_fp_overrides(overrides),
            provider_opts=clean_provider_opts(raw.get("provider_opts")),
            fingerprint_summary=dict(raw.get("fingerprint_summary") or {}),
            created_at=str(raw.get("created_at") or time.strftime("%Y-%m-%d %H:%M")),
            last_used_at=raw.get("last_used_at"),
            deleted_at=raw.get("deleted_at") or None,
        )
