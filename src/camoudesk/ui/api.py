"""JavaScript-to-Python bridge exposed through PyWebView as ``window.pywebview.api``.

Every method returns ``{"ok": True, "data": ...}`` or ``{"ok": False, "error": "..."}`` and
never raises into the webview. Methods are grouped by prefix (``profiles_*``, ``proxies_*``,
``settings_*`` ...). Proxy passwords are never sent to the page: rows carry ``has_password``
and an empty password in an update means "keep the stored one".
"""

from concurrent.futures import ThreadPoolExecutor
import functools
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import quote, unquote

from camoudesk import __version__
from camoudesk.core import geoip_db
from camoudesk.core.geo import locale_for_country
from camoudesk.core.launcher import SCREEN_PRESETS, gpu_options, resolve_network
from camoudesk.core.proxy import check_proxy_config
from camoudesk.core.proxy_parse import parse_proxy_line, parse_proxy_list
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.models.profile import Profile, ProxyConfig, validate_os_target
from camoudesk.models.proxy import SavedProxy


class _Data(dict):
    """A result that is data even though it has its own ``ok`` field (e.g. a proxy check)."""


def _bridge(fn: Callable[..., Any]) -> Callable[..., Dict[str, Any]]:
    """Wrap a method so it returns the {ok, data|error} envelope and never raises.

    Manager-style results ``{"ok": False, "error": ...}`` become bridge errors; everything
    else, including ``{"ok": True, ...}`` results and ``_Data``, is returned under ``data``.
    """

    @functools.wraps(fn)
    def wrapper(self: "DesktopBridgeApi", *args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            result = fn(self, *args, **kwargs)
        except (ValueError, KeyError, RuntimeError, OSError) as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            return {"ok": False, "error": str(message)}
        if isinstance(result, _Data):
            return {"ok": True, "data": dict(result)}
        if isinstance(result, dict) and result.get("ok") is False:
            return {"ok": False, "error": str(result.get("error") or "Operation failed")}
        return {"ok": True, "data": result}

    return wrapper


class _ProxyLookup:
    """ProxyStore-compatible ``get`` over one snapshot (avoids re-reading proxies.json per row)."""

    def __init__(self, proxies: List[SavedProxy]) -> None:
        self._by_id = {p.id: p for p in proxies}

    def get(self, proxy_id: str) -> Optional[SavedProxy]:
        return self._by_id.get(proxy_id)


def _proxy_parts(cfg: ProxyConfig) -> Dict[str, Any]:
    return {
        "type": "socks5" if cfg.scheme == "socks5h" else cfg.scheme,
        "host": cfg.host,
        "port": cfg.port,
        "username": unquote(cfg.username),
        "has_password": bool(cfg.password),
    }


def _with_locale(result: Dict[str, Any]) -> "_Data":
    """Add the locale "auto" would use for this exit country, so the form can preview it.

    Returned as data either way: a failed check is a result to show, not a bridge error.
    """
    if result.get("ok"):
        result["locale"] = locale_for_country(result.get("country_code")) or ""
    return _Data(result)


def _inline_proxy_url(parts: Dict[str, Any], current: Optional[Profile]) -> str:
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


class DesktopBridgeApi:
    """Bridge methods callable asynchronously from window.pywebview.api."""

    def __init__(
        self,
        store: ProfileStore,
        manager: ProcessManager,
        proxies: Optional[ProxyStore] = None,
        settings: Optional[SettingsStore] = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.proxies = proxies or manager.proxies
        self.settings = settings or SettingsStore(store.app_dir)
        self._checking: Set[str] = set()
        self._checking_lock = threading.Lock()
        self._notify_proxies: Callable[[], None] = lambda: None

    def set_proxy_notifier(self, callback: Callable[[], None]) -> None:
        """Called (from worker threads) when saved proxies change, e.g. as checks finish."""
        self._notify_proxies = callback

    # ------------------------------------------------------------------ serialisation

    def _row(self, profile: Profile, lookup: Any = None) -> Dict[str, Any]:
        lookup = lookup or self.proxies
        status = self.manager.get_status(profile.id)
        net = resolve_network(profile, lookup)
        data = profile.to_dict()
        data.pop("proxy", None)  # contains credentials
        if profile.proxy_id:
            saved = lookup.get(profile.proxy_id)
            mode = "saved"
            label = saved.name if saved else "Missing saved proxy"
        elif profile.proxy:
            mode = "custom"
            label = ProxyConfig.from_url(profile.proxy).masked_display()
        else:
            mode, label = "none", "Direct"
        data.update(
            {
                "proxy_mode": mode,
                "proxy_label": label,
                "proxy_parts": _proxy_parts(ProxyConfig.from_url(profile.proxy)) if profile.proxy else None,
                "proxy_missing": net.missing_saved_proxy,
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

    def _normalize(self, payload: Dict[str, Any], current: Optional[Profile] = None) -> Dict[str, Any]:
        data = dict(payload or {})
        mode = data.pop("proxy_mode", None)
        parts = data.pop("proxy_parts", None)
        if mode == "none":
            data["proxy"], data["proxy_id"] = "", ""
        elif mode == "saved":
            proxy_id = str(data.get("proxy_id") or "")
            if not self.proxies.get(proxy_id):
                raise ValueError("Choose a saved proxy")
            data["proxy"] = ""
        elif mode == "custom":
            data["proxy"] = _inline_proxy_url(parts or {}, current)
            data["proxy_id"] = ""
        return data

    # ------------------------------------------------------------------ app

    @_bridge
    def app_info(self) -> Dict[str, Any]:
        return {"version": __version__, "data_dir": str(self.store.app_dir), "geoip": geoip_db.status()}

    # ------------------------------------------------------------------ profiles

    @_bridge
    def profiles_list(self) -> List[Dict[str, Any]]:
        lookup = _ProxyLookup(self.proxies.list())
        return [self._row(p, lookup) for p in self.store.list_profiles()]

    @_bridge
    def profiles_get(self, profile_id: str) -> Dict[str, Any]:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise KeyError("Profile not found")
        return self._row(profile)

    @_bridge
    def profiles_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self.settings.get()["new_profile"]
        data = {**defaults, **self._normalize(payload or {})}
        webgl = (data.get("fp_overrides") or {}).get("webgl")
        if webgl and list(webgl) not in gpu_options(validate_os_target(data.get("os") or "macos")):
            raise ValueError("That graphics card is not available for this operating system.")
        return self._row(self.store.create_profile(data))

    @_bridge
    def profiles_update(self, profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.manager.is_running(profile_id):
            raise ValueError("Stop the running browser before editing this profile.")
        current = self.store.get_profile(profile_id)
        if current is None:
            raise KeyError("Profile not found")
        data = self._normalize(payload or {}, current)
        webgl = (data.get("fp_overrides") or {}).get("webgl")
        if webgl and list(webgl) not in gpu_options(validate_os_target(data.get("os") or current.os)):
            raise ValueError("That graphics card is not available for this operating system.")
        return self._row(self.store.update_profile(profile_id, data))

    @_bridge
    def profiles_regenerate_fingerprint(self, profile_id: str, screen: Optional[str] = None) -> Dict[str, Any]:
        """New fingerprint; ``screen`` "WxH" pins the size, "" allows any, None keeps the current pin."""
        if self.manager.is_running(profile_id):
            raise ValueError("Stop the running browser before regenerating its fingerprint.")
        if screen is None:
            return self._row(self.store.regenerate_fingerprint(profile_id))
        return self._row(self.store.regenerate_fingerprint(profile_id, screen=screen))

    @_bridge
    def fingerprint_options(self, profile_os: str) -> Dict[str, Any]:
        os_key = validate_os_target(profile_os)
        return {
            "gpus": gpu_options(os_key),
            "screens": SCREEN_PRESETS[os_key],
            "geoip_installed": bool(geoip_db.status().get("installed")),
        }

    @_bridge
    def profiles_delete(self, profile_id: str) -> Dict[str, Any]:
        stopped = self.manager.stop(profile_id)
        if not stopped.get("exited", True):
            raise RuntimeError("The browser is still running; profile data was not deleted.")
        if not self.store.delete_profile(profile_id, wipe_data=True):
            raise KeyError("Profile not found")
        return {"id": profile_id}

    @_bridge
    def profiles_launch(self, profile_id: str) -> Dict[str, Any]:
        return self.manager.launch(profile_id)

    @_bridge
    def profiles_stop(self, profile_id: str) -> Dict[str, Any]:
        """Starts a graceful stop and returns at once; the UI is notified when it finishes."""
        return self.manager.stop_async(profile_id)

    @_bridge
    def profiles_logs(self, profile_id: str, max_lines: int = 300) -> str:
        return self.manager.read_logs(profile_id, max_lines=max(1, min(int(max_lines), 2000)))

    @_bridge
    def profiles_diagnostics(self, profile_id: str) -> str:
        return self.manager.diagnostics(profile_id)

    # ------------------------------------------------------------------ proxies

    def _proxy_row(self, proxy: SavedProxy, usage: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        data = proxy.to_public_dict()
        if usage is None:
            usage = self._proxy_usage()
        data["used_by"] = usage.get(proxy.id, 0)
        data["checking"] = proxy.id in self._checking
        return data

    def _proxy_usage(self) -> Dict[str, int]:
        usage: Dict[str, int] = {}
        for p in self.store.list_profiles():
            if p.proxy_id:
                usage[p.proxy_id] = usage.get(p.proxy_id, 0) + 1
        return usage

    def _check_saved(self, proxy_id: str) -> Dict[str, Any]:
        saved = self.proxies.get(proxy_id)
        if saved is None:
            raise KeyError("Saved proxy not found")
        with self._checking_lock:
            self._checking.add(proxy_id)
        self._notify_proxies()
        try:
            result = check_proxy_config(saved.to_config())
            self.proxies.record_check(proxy_id, result)
            return result
        finally:
            with self._checking_lock:
                self._checking.discard(proxy_id)
            self._notify_proxies()

    @_bridge
    def proxies_list(self) -> List[Dict[str, Any]]:
        usage = self._proxy_usage()
        return [self._proxy_row(p, usage) for p in self.proxies.list()]

    @_bridge
    def proxies_create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._proxy_row(self.proxies.create(data or {}))

    @_bridge
    def proxies_update(self, proxy_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._proxy_row(self.proxies.update(proxy_id, data or {}))

    @_bridge
    def proxies_delete(self, proxy_id: str, force: bool = False) -> Dict[str, Any]:
        """Delete a saved proxy. Profiles using it are NOT switched to Direct (that would expose
        the real IP); they refuse to launch until another proxy is chosen."""
        in_use = self._proxy_usage().get(proxy_id, 0)
        if in_use and not force:
            return _Data({"deleted": False, "in_use": in_use})
        if not self.proxies.delete(proxy_id):
            raise KeyError("Saved proxy not found")
        return _Data({"deleted": True, "in_use": in_use})

    @_bridge
    def proxies_check(self, proxy_id: str) -> Dict[str, Any]:
        result = self._check_saved(proxy_id)
        row = self._proxy_row(self.proxies.get(proxy_id))
        row["result"] = _with_locale(result)
        return row

    @_bridge
    def proxies_check_all(self, proxy_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Check proxies in the background (8 at a time); the page is notified as each finishes."""
        ids = [p.id for p in self.proxies.list() if not proxy_ids or p.id in proxy_ids]

        def run() -> None:
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="camoudesk-proxy-check") as pool:
                for future in [pool.submit(self._check_saved, pid) for pid in ids]:
                    try:
                        future.result()
                    except Exception:
                        pass

        threading.Thread(target=run, name="camoudesk-check-all", daemon=True).start()
        return {"started": len(ids)}

    @_bridge
    def proxies_import_preview(self, text: str, default_scheme: str = "http") -> Dict[str, Any]:
        configs, errors = parse_proxy_list(str(text or ""), default_scheme or "http")
        return {
            "proxies": [{"display": c.masked_display(), "type": c.scheme, "host": c.host, "port": c.port} for c in configs],
            "errors": [{"line": e.line_no, "text": e.line[:120], "error": e.error} for e in errors],
        }

    @_bridge
    def proxies_import(self, text: str, default_scheme: str = "http", name_prefix: str = "", check: bool = True) -> Dict[str, Any]:
        created, errors = self.proxies.import_text(str(text or ""), default_scheme or "http", str(name_prefix or "")[:40])
        if check and created:
            self.proxies_check_all([p.id for p in created])
        return {
            "created": len(created),
            "errors": [{"line": e.line_no, "text": e.line[:120], "error": e.error} for e in errors],
        }

    @_bridge
    def proxies_rotate(self, proxy_id: str) -> Dict[str, Any]:
        """Call the provider's change-IP URL (directly, not through the proxy), then re-check."""
        saved = self.proxies.get(proxy_id)
        if saved is None:
            raise KeyError("Saved proxy not found")
        if not saved.change_ip_url:
            raise ValueError("This proxy has no change-IP URL")
        import requests

        try:
            resp = requests.get(saved.change_ip_url, timeout=20, headers={"User-Agent": "CamouDesk/1.0"})
        except requests.RequestException as exc:
            raise RuntimeError(f"Change-IP request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"Change-IP URL returned HTTP {resp.status_code}")
        time.sleep(2)  # providers usually need a moment before the new IP is live
        result = self._check_saved(proxy_id)
        row = self._proxy_row(self.proxies.get(proxy_id))
        row["result"] = _with_locale(result)
        return row

    @_bridge
    def profiles_assign_proxy(self, profile_ids: List[str], proxy_id: str) -> Dict[str, Any]:
        """Bulk: point profiles at a saved proxy ("" = Direct). Running profiles are skipped."""
        if proxy_id and not self.proxies.get(proxy_id):
            raise KeyError("Saved proxy not found")
        changed, skipped = 0, 0
        for pid in profile_ids or []:
            if self.manager.is_running(pid):
                skipped += 1
                continue
            self.store.update_profile(pid, {"proxy_id": proxy_id, "proxy": ""} if proxy_id else {"proxy_id": "", "proxy": ""})
            changed += 1
        return {"changed": changed, "skipped_running": skipped}

    @_bridge
    def proxy_parse(self, line: str, default_scheme: str = "http") -> Dict[str, Any]:
        """Split a pasted provider line into form fields (echoes back what the user pasted)."""
        cfg = parse_proxy_line(str(line or ""), default_scheme or "http")
        parts = _proxy_parts(cfg)
        parts["password"] = unquote(cfg.password)
        return parts

    @_bridge
    def proxy_test(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Check a proxy before saving: {proxy_id} | {proxy_parts, profile_id?} | {url}."""
        payload = payload or {}
        if payload.get("proxy_id"):
            saved = self.proxies.get(str(payload["proxy_id"]))
            if saved is None:
                raise KeyError("Saved proxy not found")
            result = check_proxy_config(saved.to_config())
            self.proxies.record_check(saved.id, result)
            return _with_locale(result)
        if payload.get("proxy_parts"):
            current = self.store.get_profile(payload["profile_id"]) if payload.get("profile_id") else None
            cfg = ProxyConfig.from_url(_inline_proxy_url(payload["proxy_parts"], current))
        else:
            cfg = ProxyConfig.from_url(str(payload.get("url") or ""))
        if not cfg.is_configured:
            raise ValueError("Enter a proxy first")
        return _with_locale(check_proxy_config(cfg))

    # ------------------------------------------------------------------ settings

    @_bridge
    def settings_get(self) -> Dict[str, Any]:
        return self.settings.get()

    @_bridge
    def settings_update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self.settings.update(patch or {})

    @_bridge
    def geoip_download(self) -> Dict[str, Any]:
        return geoip_db.download()
