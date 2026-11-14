"""What the UI and the REST API show for profiles and proxies (never credentials)."""

from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional
from urllib.parse import quote, unquote

from camoudesk.core.geo import locale_for_country
from camoudesk.core.launcher import resolve_network
from camoudesk.models.profile import Profile, ProxyConfig
from camoudesk.models.proxy import SavedProxy

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext


class ProxyLookup:
    """ProxyStore-compatible ``get`` over one snapshot (avoids re-reading proxies.json per row)."""

    def __init__(self, proxies: Iterable[SavedProxy]) -> None:
        self._by_id = {p.id: p for p in proxies}

    def get(self, proxy_id: str) -> Optional[SavedProxy]:
        return self._by_id.get(proxy_id)


def proxy_parts(cfg: ProxyConfig) -> Dict[str, Any]:
    return {
        "type": "socks5" if cfg.scheme == "socks5h" else cfg.scheme,
        "host": cfg.host,
        "port": cfg.port,
        "username": unquote(cfg.username),
        "has_password": bool(cfg.password),
    }


def with_locale(result: Dict[str, Any]) -> Dict[str, Any]:
    """Add the locale "auto" would use for this exit country, so the form can preview it."""
    if result.get("ok"):
        result["locale"] = locale_for_country(result.get("country_code")) or ""
    return result


def inline_proxy_url(parts: Dict[str, Any], current: Optional[Profile]) -> str:
    """Build an inline proxy URL from form fields; a blank password keeps the stored one."""
    ptype = str(parts.get("type") or "http").lower()
    host = str(parts.get("host") or "").strip().strip("[]")
    port = str(parts.get("port") or "").strip()
    if not host or not port:
        raise ValueError("Custom proxy needs a host and a port")
    username = str(parts.get("username") or "")
    password = str(parts.get("password") or "")
    if not password and username and current is not None and current.proxy and not parts.get("clear_password"):
        old = ProxyConfig.from_url(current.proxy)
        if old.host == host and unquote(old.username) == username:
            password = unquote(old.password)
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if (username or password) else ""
    host_part = f"[{host}]" if ":" in host else host
    return ProxyConfig.from_url(f"{ptype}://{auth}{host_part}:{port}").raw_url


def identity_summary(ctx: "AppContext", profile: Profile) -> Optional[Dict[str, Any]]:
    from camoudesk.core import identity_check

    report = identity_check.load_report(ctx.store.profile_dir(profile.id))
    if not report:
        return None
    return {k: report.get(k) for k in ("status", "passed", "total", "checked_at")}


def canvas_real(ctx: "AppContext", profile: Profile) -> bool:
    """Whether this profile shows its real canvas (it claims this Mac's OS and GPU)."""
    try:
        saved = ctx.engine.load_identity(ctx.store.profile_dir(profile.id)) or {}
        return ctx.engine.canvas_policy(ctx.store.app_dir, profile, saved) is not True
    except Exception:
        return False


def profile_row(ctx: "AppContext", profile: Profile, lookup: Any = None) -> Dict[str, Any]:
    lookup = lookup or ctx.proxies
    status = ctx.manager.get_status(profile.id)
    net = resolve_network(profile, lookup)
    data = profile.to_dict()
    data.pop("proxy", None)  # contains credentials
    provider = None
    if profile.proxy_id:
        saved = lookup.get(profile.proxy_id)
        mode = "saved"
        label = saved.name if saved else "Missing saved proxy"
        if saved is not None and saved.provider:
            opts = saved.profile_opts(profile)
            provider = {
                "name": saved.provider.get("name", "evomi"),
                "product": opts.get("product", ""),
                "summary": saved.adapter.summary(opts),
                "session": "" if opts.get("mode") == "rotating" else saved.session_for(profile),
                "mode": opts.get("mode", "sticky"),
                "country": opts.get("country", ""),
                "exit_ip": net.exit_ip,
            }
    elif profile.proxy:
        mode = "custom"
        label = ProxyConfig.from_url(profile.proxy).masked_display()
    else:
        mode, label = "none", "Direct"
    data.update(
        {
            "proxy_mode": mode,
            "proxy_label": label,
            "proxy_parts": proxy_parts(ProxyConfig.from_url(profile.proxy)) if profile.proxy else None,
            "proxy_missing": net.missing_saved_proxy,
            "provider": provider,
            "canvas_real": canvas_real(ctx, profile),
            "identity": identity_summary(ctx, profile),
            "proxy_country": net.country_code,
            "effective_timezone": net.timezone,
            "effective_locale": net.locale,
            "status": status["status"],
            "pid": status.get("pid"),
            "mode": status.get("mode"),
            "last_error": status.get("last_error", ""),
            "warning": status.get("warning", ""),
        }
    )
    return data


def proxy_usage(ctx: "AppContext") -> Dict[str, int]:
    usage: Dict[str, int] = {}
    for p in ctx.store.list_profiles():
        if p.proxy_id and not p.deleted_at:
            usage[p.proxy_id] = usage.get(p.proxy_id, 0) + 1
    return usage


def proxy_row(proxy: SavedProxy, usage: Dict[str, int], checking: Iterable[str] = ()) -> Dict[str, Any]:
    data = proxy.to_public_dict()
    data["used_by"] = usage.get(proxy.id, 0)
    data["checking"] = proxy.id in set(checking)
    return data


def parse_errors(errors: List[Any]) -> List[Dict[str, Any]]:
    return [{"line": e.line_no, "text": e.line[:120], "error": e.error} for e in errors]
