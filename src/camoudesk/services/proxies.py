"""ProxyService: saved proxies, checks, imports, rotation and provider integrations (Evomi)."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from urllib.parse import unquote

from camoudesk.core.launcher import resolve_network, session_key
from camoudesk.core.proxy import check_proxy_config
from camoudesk.core.proxy_parse import parse_proxy_line, parse_proxy_list
from camoudesk.core import providers
from camoudesk.core.providers import ProviderApiError
from camoudesk.models.profile import ProxyConfig
from camoudesk.models.proxy import SavedProxy
from camoudesk.services.errors import Conflict, Invalid, NotFound
from camoudesk.services.views import inline_proxy_url, parse_errors, proxy_parts, proxy_row, proxy_usage, with_locale

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext


class ProxyService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.checking: Set[str] = set()
        self._checking_lock = threading.Lock()

    def _changed(self) -> None:
        self.ctx.events.publish("proxies")

    def _get(self, proxy_id: str) -> SavedProxy:
        saved = self.ctx.proxies.get(proxy_id)
        if saved is None:
            raise NotFound("Saved proxy not found")
        return saved

    def row(self, proxy: SavedProxy, usage: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        return proxy_row(proxy, usage if usage is not None else proxy_usage(self.ctx), self.checking)

    # ---------------------------------------------------------------- CRUD

    def rows(self) -> List[Dict[str, Any]]:
        usage = proxy_usage(self.ctx)
        return [self.row(p, usage) for p in self.ctx.proxies.list()]

    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.row(self.ctx.proxies.create(data or {}))

    def update(self, proxy_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.row(self.ctx.proxies.update(proxy_id, data or {}))

    def delete(self, proxy_id: str, force: bool = False) -> Dict[str, Any]:
        """Delete a saved proxy. Profiles using it are NOT switched to Direct (that would expose
        the real IP); they refuse to launch until another proxy is chosen."""
        in_use = proxy_usage(self.ctx).get(proxy_id, 0)
        if in_use and not force:
            return {"deleted": False, "in_use": in_use}
        if not self.ctx.proxies.delete(proxy_id):
            raise NotFound("Saved proxy not found")
        return {"deleted": True, "in_use": in_use}

    # ---------------------------------------------------------------- checks

    def check_saved(self, proxy_id: str) -> Dict[str, Any]:
        saved = self._get(proxy_id)
        with self._checking_lock:
            self.checking.add(proxy_id)
        self._changed()
        try:
            result = check_proxy_config(saved.to_config())
            self.ctx.proxies.record_check(proxy_id, result)
            return result
        finally:
            with self._checking_lock:
                self.checking.discard(proxy_id)
            self._changed()

    def check(self, proxy_id: str) -> Dict[str, Any]:
        result = self.check_saved(proxy_id)
        row = self.row(self._get(proxy_id))
        row["result"] = with_locale(result)
        return row

    def check_all(self, proxy_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Check proxies in the background (4 at a time); listeners are notified as each finishes."""
        ids = [p.id for p in self.ctx.proxies.list() if not proxy_ids or p.id in proxy_ids]

        def run() -> None:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="camoudesk-proxy-check") as pool:
                for future in [pool.submit(self.check_saved, pid) for pid in ids]:
                    try:
                        future.result()
                    except Exception:
                        pass

        threading.Thread(target=run, name="camoudesk-check-all", daemon=True).start()
        return {"started": len(ids)}

    def rotate(self, proxy_id: str) -> Dict[str, Any]:
        """Call the provider's change-IP URL (directly, not through the proxy), then re-check."""
        saved = self._get(proxy_id)
        if not saved.change_ip_url:
            raise Invalid("This proxy has no change-IP URL")
        import requests

        try:
            resp = requests.get(saved.change_ip_url, timeout=20, headers={"User-Agent": "CamouDesk/1.0"})
        except requests.RequestException as exc:
            raise Conflict(f"Change-IP request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise Conflict(f"Change-IP URL returned HTTP {resp.status_code}")
        time.sleep(2)  # providers usually need a moment before the new IP is live
        return self.check(proxy_id)

    # ---------------------------------------------------------------- import / parse

    @staticmethod
    def import_preview(text: str, default_scheme: str = "http") -> Dict[str, Any]:
        configs, errors = parse_proxy_list(str(text or ""), default_scheme or "http")
        return {
            "proxies": [{"display": c.masked_display(), "type": c.scheme, "host": c.host, "port": c.port} for c in configs],
            "errors": parse_errors(errors),
        }

    def import_text(self, text: str, default_scheme: str = "http", name_prefix: str = "", check: bool = True) -> Dict[str, Any]:
        created, errors = self.ctx.proxies.import_text(str(text or ""), default_scheme or "http", str(name_prefix or "")[:40])
        if check and created:
            self.check_all([p.id for p in created])
        return {"created": len(created), "errors": parse_errors(errors)}

    @staticmethod
    def parse(line: str, default_scheme: str = "http") -> Dict[str, Any]:
        """Split a pasted provider line into form fields (echoes back what the user pasted)."""
        cfg = parse_proxy_line(str(line or ""), default_scheme or "http")
        parts = proxy_parts(cfg)
        parts["password"] = unquote(cfg.password)
        found = providers.detect(cfg.host)
        if found:
            # A provider gateway: the password's options become provider settings (its session is per profile).
            adapter, product = found
            parts["password"], params = adapter.split_password(parts["password"])
            parts["provider"] = adapter.opts_from_params(params, product)
        return parts

    def test(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Check a proxy before saving: {proxy_id} | {proxy_parts, profile_id?} | {url}."""
        payload = payload or {}
        if payload.get("proxy_id"):
            saved = self._get(str(payload["proxy_id"]))
            if saved.provider:
                return with_locale(self.check_provider_session(saved, payload))
            result = check_proxy_config(saved.to_config())
            self.ctx.proxies.record_check(saved.id, result)
            return with_locale(result)
        if payload.get("proxy_parts"):
            current = self.ctx.store.get_profile(payload["profile_id"]) if payload.get("profile_id") else None
            cfg = ProxyConfig.from_url(inline_proxy_url(payload["proxy_parts"], current))
        else:
            cfg = ProxyConfig.from_url(str(payload.get("url") or ""))
        if not cfg.is_configured:
            raise Invalid("Enter a proxy first")
        return with_locale(check_proxy_config(cfg))

    def check_provider_session(self, saved: SavedProxy, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Check the exact session a profile will use, including unsaved targeting from the form."""
        store = self.ctx.store
        current = store.get_profile(payload["profile_id"]) if payload.get("profile_id") else None
        stored = dict(current.provider_opts) if current is not None and current.proxy_id == saved.id else {}
        draft = payload.get("provider_opts") if isinstance(payload.get("provider_opts"), dict) else None
        opts = dict(stored)
        if draft is not None:
            opts = {k: v for k, v in stored.items() if k == "rotation"}
            opts.update({k: v for k, v in draft.items() if v not in (None, "") and k not in ("rotation", "checked")})
        probe = SimpleNamespace(id=current.id if current is not None else "draft", provider_opts=opts)
        cfg = saved.config_for(probe)
        result = check_proxy_config(cfg)
        eff = saved.profile_opts(probe)
        place = eff.get("city") or eff.get("region")
        if not result.get("ok") and place and "credentials" not in str(result.get("error")) and "407" not in str(result.get("error")):
            # Evomi refuses the connection when it has no IPs for a city/region right now.
            where = place.replace(".", " ").title()
            result["error"] = (f"Evomi has no IPs in {where} right now ({result.get('error')}). "
                               "Try a nearby city, only the region or country, or another time.")
        if current is not None and result.get("ok"):
            net = resolve_network(current, self.ctx.proxies)
            if net.session_key and net.session_key == session_key(cfg):  # what it will launch with
                store.record_session_check(current.id, result, net.session_key)
        result["session"] = "" if eff.get("mode") == "rotating" else saved.session_for(probe)
        if current is None:
            result["note"] = "A new profile gets its own session (and IP) once it is created."
        return result

    # ---------------------------------------------------------------- provider integrations

    @staticmethod
    def _adapter(name: str) -> "providers.ProviderAdapter":
        try:
            return providers.get(name)
        except ValueError as exc:
            raise Invalid(f"Unknown provider '{name}'") from exc

    def providers_status(self) -> Dict[str, Any]:
        return self.ctx.providers.status()

    def set_provider_key(self, name: str, api_key: str) -> Dict[str, Any]:
        """Save an API key after the provider accepts it (it is never sent back to the page)."""
        adapter = self._adapter(name)
        key = str(api_key or "").strip()
        try:
            targets = adapter.fetch_targets(key, next(iter(adapter.products)))
        except ProviderApiError as exc:
            raise Invalid(str(exc)) from exc
        store = self.ctx.providers
        store.set_api_key(name, key)
        store.store_targets(name, next(iter(adapter.products)), targets)
        return {**store.status()[name], "countries": len(targets["countries"])}

    def clear_provider_key(self, name: str) -> Dict[str, Any]:
        self.ctx.providers.clear_api_key(name)
        return self.ctx.providers.status()

    def provider_targets(self, name: str = "evomi", product: str = "rpc", refresh: bool = False) -> Dict[str, Any]:
        """Countries, regions and cities for the pickers: from Evomi's API with a key (cached a
        day), otherwise every country code CamouDesk knows, with free-text regions/cities."""
        adapter = self._adapter(name)
        product = product if product in adapter.products else next(iter(adapter.products))
        store = self.ctx.providers
        key = store.api_key(name)
        fallback_warning = ""
        if key:
            cached = None if refresh else store.cached_targets(name, product)
            if cached is not None:
                return {**cached, "source": name}
            try:
                targets = adapter.fetch_targets(key, product)
                store.store_targets(name, product, targets)
                return {**targets, "source": name}
            except ProviderApiError as exc:
                stale = store.cached_targets(name, product, max_age=float("inf"))
                if stale is not None:
                    return {**stale, "source": name, "warning": str(exc)}
                fallback_warning = str(exc)
        from camoudesk.core.geo import _territory_languages

        return {"countries": sorted(_territory_languages().keys()), "regions": {}, "cities": {}, "source": "builtin",
                "warning": fallback_warning}
