"""Saved proxies: reusable, named proxy entries shared across profiles."""

from dataclasses import asdict, dataclass, field
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from camoudesk.core.providers import evomi
from camoudesk.models.profile import ProxyConfig, clean_tags, normalize_country, normalize_ip, sanitize_profile_id

PROXY_TYPES = ("http", "https", "socks4", "socks5")


@dataclass
class ProxyCheck:
    ok: bool = False
    ip: str = ""
    country: str = ""
    country_code: str = ""
    city: str = ""
    timezone: str = ""
    latency_ms: Optional[int] = None
    error: str = ""
    at: str = ""

    @classmethod
    def from_result(cls, res: Dict[str, Any]) -> "ProxyCheck":
        return cls(
            ok=bool(res.get("ok")),
            ip=normalize_ip(res.get("ip")),
            country=str(res.get("country") or "")[:80],
            country_code=normalize_country(res.get("country_code")),
            city=str(res.get("city") or "")[:80],
            timezone=str(res.get("timezone") or "")[:64],
            latency_ms=res.get("latency_ms") if isinstance(res.get("latency_ms"), int) else None,
            error=str(res.get("error") or "")[:300],
            at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    @classmethod
    def from_dict(cls, raw: Any) -> "ProxyCheck":
        if not isinstance(raw, dict):
            return cls()
        chk = cls.from_result(raw)
        chk.at = str(raw.get("at") or "")
        return chk


@dataclass
class SavedProxy:
    id: str
    name: str
    type: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    change_ip_url: str = ""
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    last_check: ProxyCheck = field(default_factory=ProxyCheck)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    # Provider integration (core/providers): for Evomi, ``password`` is the account's base password
    # and targeting/session options live here; each profile composes its own session at launch.
    provider: Dict[str, Any] = field(default_factory=dict)

    def _config(self, password: str) -> ProxyConfig:
        auth = ""
        if self.username or password:
            auth = f"{quote(self.username, safe='')}:{quote(password, safe='')}@"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return ProxyConfig.from_url(f"{self.type}://{auth}{host}:{self.port}")

    def to_config(self) -> ProxyConfig:
        """The proxy itself. For a provider entry: its targeting without a session (one IP per request)."""
        if self.provider:
            return self._config(evomi.compose_password(self.password, self.provider))
        return self._config(self.password)

    def profile_opts(self, profile: Any) -> Dict[str, Any]:
        """Provider options for a profile: this proxy's defaults with the profile's overrides."""
        return evomi.effective_opts(self.provider, getattr(profile, "provider_opts", None) or {})

    def session_for(self, profile: Any) -> str:
        rotation = int((getattr(profile, "provider_opts", None) or {}).get("rotation") or 0)
        return evomi.session_id(profile.id, rotation)

    def config_for(self, profile: Any) -> ProxyConfig:
        """What ``profile`` routes through: its own provider session, or the plain proxy."""
        if not self.provider:
            return self.to_config()
        return self._config(evomi.compose_password(self.password, self.profile_opts(profile), self.session_for(profile)))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> Dict[str, Any]:
        """For the UI list: the password is never sent, only whether one is set."""
        data = self.to_dict()
        data["password"] = ""
        data["has_password"] = bool(self.password)
        data["display"] = self.to_config().masked_display()
        if self.provider:
            data["provider_summary"] = evomi.summary(self.provider)
        return data

    @classmethod
    def from_config(cls, proxy_id: str, name: str, cfg: ProxyConfig, **extra: Any) -> "SavedProxy":
        from urllib.parse import unquote

        return cls.from_dict(
            {
                "id": proxy_id,
                "name": name,
                "type": "socks5" if cfg.scheme == "socks5h" else cfg.scheme,
                "host": cfg.host,
                "port": cfg.port,
                "username": unquote(cfg.username),
                "password": unquote(cfg.password),
                **extra,
            }
        )

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SavedProxy":
        proxy_id = sanitize_profile_id(str(raw["id"]))
        ptype = str(raw.get("type") or "http").lower()
        if ptype not in PROXY_TYPES:
            raise ValueError(f"Unsupported proxy type '{ptype}'")
        host = str(raw.get("host") or "").strip().strip("[]")
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Port must be a number") from exc
        if not host or not (0 < port < 65536):
            raise ValueError("Proxy needs a host and a port between 1 and 65535")
        username = str(raw.get("username") or "")
        password = str(raw.get("password") or "")
        if ptype == "socks4" and (username or password):
            raise ValueError("SOCKS4 proxies cannot use a username/password; use SOCKS5 instead.")
        provider: Dict[str, Any] = {}
        product = evomi.detect(host)
        if product:
            # Options pasted inside the password become provider settings (the pasted session ID is
            # dropped: every profile gets its own). Explicit provider settings from the form win.
            password, params = evomi.split_password(password)
            given = raw.get("provider")
            if isinstance(given, dict) and given:
                provider = evomi.clean_opts({**given, "product": product})
            else:
                provider = evomi.opts_from_params(params, product)
        change_url = str(raw.get("change_ip_url") or "").strip()
        if change_url and urlsplit(change_url).scheme not in ("http", "https"):
            raise ValueError("Change-IP URL must be http(s)")
        proxy = cls(
            id=proxy_id,
            name=(str(raw.get("name") or "").strip() or f"{host}:{port}")[:80],
            type=ptype,
            host=host,
            port=port,
            username=username,
            password=password,
            change_ip_url=change_url,
            notes=str(raw.get("notes") or "").strip()[:500],
            tags=clean_tags(raw.get("tags")),
            last_check=ProxyCheck.from_dict(raw.get("last_check")),
            created_at=str(raw.get("created_at") or time.strftime("%Y-%m-%d %H:%M")),
            provider=provider,
        )
        proxy.to_config()  # validates host/port/scheme combination
        return proxy
