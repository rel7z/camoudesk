"""JavaScript-to-Python bridge exposed through PyWebView as ``window.pywebview.api``.

A thin interface over the services (camoudesk/services): it adapts arguments, opens native file
dialogs, and wraps results. Every method returns ``{"ok": True, "data": ...}`` or
``{"ok": False, "error": "..."}`` and never raises into the webview. Methods are grouped by
prefix (``profiles_*``, ``proxies_*``, ``settings_*`` ...). Proxy passwords are never sent to the
page: rows carry ``has_password`` and an empty password in an update means "keep the stored one".
"""

import functools
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from camoudesk import __version__
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.services.context import AppContext
from camoudesk.services.launch import TEST_SITES


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


class DesktopBridgeApi:
    """Bridge methods callable asynchronously from window.pywebview.api."""

    TEST_SITES = TEST_SITES

    def __init__(
        self,
        store: Optional[ProfileStore] = None,
        manager: Optional[ProcessManager] = None,
        proxies: Optional[ProxyStore] = None,
        settings: Optional[SettingsStore] = None,
        ctx: Optional[AppContext] = None,
    ) -> None:
        self.ctx = ctx or AppContext.create(store=store, manager=manager, proxies=proxies, settings=settings)
        self._window: Any = None
        self._api_server: Any = None
        self._api_error = ""
        self.ctx.events.subscribe("settings", lambda settings: self.sync_api_server(settings))

    # Stores and manager, for the UI host and tests.
    @property
    def store(self) -> ProfileStore:
        return self.ctx.store

    @property
    def manager(self) -> ProcessManager:
        return self.ctx.manager

    @property
    def proxies(self) -> ProxyStore:
        return self.ctx.proxies

    @property
    def settings(self) -> SettingsStore:
        return self.ctx.settings

    @property
    def providers(self) -> Any:
        return self.ctx.providers

    # ---------------------------------------------------------------- host wiring

    def set_window(self, window: Any) -> None:
        """The pywebview window, used for native open/save dialogs."""
        self._window = window

    def _dialog(self, kind: str, **kwargs: Any) -> Any:
        import webview

        if self._window is None:
            raise RuntimeError("File dialogs are only available in the desktop app")
        dialog_type = {"open": webview.FileDialog.OPEN, "save": webview.FileDialog.SAVE, "folder": webview.FileDialog.FOLDER}[kind]
        result = self._window.create_file_dialog(dialog_type, **kwargs)
        if not result:
            return None
        if kwargs.get("allow_multiple"):
            return list(result)
        return result if isinstance(result, str) else result[0]

    def set_theme_listener(self, callback: Callable[[str], None]) -> None:
        """Called with the new theme when Settings → Appearance changes (native window chrome)."""
        self.ctx.events.subscribe("theme", lambda theme: callback(theme))

    def set_proxy_notifier(self, callback: Callable[[], None]) -> None:
        """Called (from worker threads) when saved proxies change, e.g. as checks finish."""
        self.ctx.events.subscribe("proxies", lambda **_: callback())

    # ---------------------------------------------------------------- app

    @_bridge
    def app_info(self) -> Dict[str, Any]:
        return {"version": __version__, "data_dir": str(self.store.app_dir), "geoip": self.ctx.settings_service.geoip_status()}

    # ---------------------------------------------------------------- profiles

    @_bridge
    def profiles_list(self) -> List[Dict[str, Any]]:
        return self.ctx.profiles_service.rows(trashed=False)

    @_bridge
    def profiles_trash_list(self) -> List[Dict[str, Any]]:
        return self.ctx.profiles_service.rows(trashed=True)

    @_bridge
    def profiles_trash(self, profile_ids: List[str]) -> Dict[str, Any]:
        return self.ctx.profiles_service.trash(profile_ids)

    @_bridge
    def profiles_restore(self, profile_ids: List[str]) -> Dict[str, Any]:
        return self.ctx.profiles_service.restore(profile_ids)

    @_bridge
    def profiles_purge(self, profile_ids: List[str]) -> Dict[str, Any]:
        return self.ctx.profiles_service.purge(profile_ids)

    @_bridge
    def profiles_empty_trash(self) -> Dict[str, Any]:
        return self.ctx.profiles_service.empty_trash()

    @_bridge
    def profiles_clone(self, profile_id: str, copy_data: bool = False, name: str = "") -> Dict[str, Any]:
        return self.ctx.profiles_service.clone(profile_id, copy_data, name)

    @_bridge
    def profiles_bulk_meta(self, profile_ids: List[str], patch: Dict[str, Any]) -> Dict[str, Any]:
        return self.ctx.profiles_service.bulk_meta(profile_ids, patch)

    @_bridge
    def folders_rename(self, old: str, new: str) -> Dict[str, Any]:
        return self.ctx.profiles_service.rename_folder(old, new)

    @_bridge
    def profiles_get(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.profiles_service.get(profile_id)

    @_bridge
    def fingerprint_preview(self, profile_os: str, screen: Optional[str] = None, source: str = "generated",
                            match_host: bool = False) -> Dict[str, Any]:
        return self.ctx.profiles_service.preview_fingerprint(profile_os, screen, source, match_host)

    @_bridge
    def profiles_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.ctx.profiles_service.create(payload)

    @_bridge
    def profiles_update(self, profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.ctx.profiles_service.update(profile_id, payload)

    @_bridge
    def profiles_regenerate_fingerprint(self, profile_id: str, screen: Optional[str] = None,
                                        source: Optional[str] = None) -> Dict[str, Any]:
        return self.ctx.profiles_service.regenerate_fingerprint(profile_id, screen, source)

    @_bridge
    def fingerprint_options(self, profile_os: str) -> Dict[str, Any]:
        return self.ctx.profiles_service.fingerprint_options(profile_os)

    @_bridge
    def profiles_delete(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.profiles_service.delete(profile_id)

    @_bridge
    def profiles_assign_proxy(self, profile_ids: List[str], proxy_id: str) -> Dict[str, Any]:
        return self.ctx.profiles_service.assign_proxy(profile_ids, proxy_id)

    @_bridge
    def profiles_new_ip(self, profile_ids: List[str]) -> Dict[str, Any]:
        return self.ctx.profiles_service.new_ip(profile_ids)

    # ---------------------------------------------------------------- launching

    @_bridge
    def profiles_launch(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.launch_service.launch(profile_id)

    @_bridge
    def profiles_stop(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.launch_service.stop(profile_id)

    @_bridge
    def profiles_logs(self, profile_id: str, max_lines: int = 300) -> str:
        return self.ctx.launch_service.logs(profile_id, max_lines)

    @_bridge
    def profiles_diagnostics(self, profile_id: str) -> str:
        return self.ctx.launch_service.diagnostics(profile_id)

    @_bridge
    def profiles_open_test_sites(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.launch_service.open_test_sites(profile_id)

    # ---------------------------------------------------------------- anonymity check

    @_bridge
    def profiles_check_identity(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.identity_service.check(profile_id)

    @_bridge
    def profiles_identity_report(self, profile_id: str) -> Optional[Dict[str, Any]]:
        return self.ctx.identity_service.report(profile_id)

    # ---------------------------------------------------------------- proxies

    @_bridge
    def proxies_list(self) -> List[Dict[str, Any]]:
        return self.ctx.proxy_service.rows()

    @_bridge
    def proxies_create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.ctx.proxy_service.create(data)

    @_bridge
    def proxies_update(self, proxy_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.ctx.proxy_service.update(proxy_id, data)

    @_bridge
    def proxies_delete(self, proxy_id: str, force: bool = False) -> Dict[str, Any]:
        return _Data(self.ctx.proxy_service.delete(proxy_id, force))

    @_bridge
    def proxies_check(self, proxy_id: str) -> Dict[str, Any]:
        return self._with_data_result(self.ctx.proxy_service.check(proxy_id))

    @_bridge
    def proxies_check_all(self, proxy_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.ctx.proxy_service.check_all(proxy_ids)

    @_bridge
    def proxies_import_preview(self, text: str, default_scheme: str = "http") -> Dict[str, Any]:
        return self.ctx.proxy_service.import_preview(text, default_scheme)

    @_bridge
    def proxies_import(self, text: str, default_scheme: str = "http", name_prefix: str = "", check: bool = True) -> Dict[str, Any]:
        return self.ctx.proxy_service.import_text(text, default_scheme, name_prefix, check)

    @_bridge
    def proxies_rotate(self, proxy_id: str) -> Dict[str, Any]:
        return self._with_data_result(self.ctx.proxy_service.rotate(proxy_id))

    @_bridge
    def proxy_parse(self, line: str, default_scheme: str = "http") -> Dict[str, Any]:
        return self.ctx.proxy_service.parse(line, default_scheme)

    @_bridge
    def proxy_test(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # A failed check is a result to show, not a bridge error.
        return _Data(self.ctx.proxy_service.test(payload))

    @staticmethod
    def _with_data_result(row: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(row.get("result"), dict):
            row["result"] = _Data(row["result"])
        return row

    # ---------------------------------------------------------------- provider integrations

    @_bridge
    def providers_status(self) -> Dict[str, Any]:
        return self.ctx.proxy_service.providers_status()

    @_bridge
    def providers_set_key(self, name: str, api_key: str) -> Dict[str, Any]:
        return self.ctx.proxy_service.set_provider_key(name, api_key)

    @_bridge
    def providers_clear_key(self, name: str) -> Dict[str, Any]:
        return self.ctx.proxy_service.clear_provider_key(name)

    @_bridge
    def providers_targets(self, name: str = "evomi", product: str = "rpc", refresh: bool = False) -> Dict[str, Any]:
        return self.ctx.proxy_service.provider_targets(name, product, refresh)

    # ---------------------------------------------------------------- cookies

    @_bridge
    def cookies_summary(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.cookie_service.summary(profile_id)

    @_bridge
    def cookies_export_text(self, profile_id: str, fmt: str = "json") -> str:
        return self.ctx.cookie_service.export_text(profile_id, fmt)

    @_bridge
    def cookies_save(self, profile_id: str, fmt: str = "json") -> Dict[str, Any]:
        cookies = self.ctx.cookie_service
        path = self._dialog("save", save_filename=cookies.default_filename(profile_id, fmt))
        if not path:
            return {"saved": False}
        return cookies.save(profile_id, path, fmt)

    @_bridge
    def cookies_import_text(self, profile_id: str, text: str) -> Dict[str, Any]:
        return self.ctx.cookie_service.import_text(profile_id, text)

    @_bridge
    def cookies_import_file(self, profile_id: str) -> Dict[str, Any]:
        path = self._dialog("open", file_types=("Cookie files (*.json;*.txt)", "All files (*.*)"))
        if not path:
            return {"imported": 0, "cancelled": True}
        return self.ctx.cookie_service.import_file(profile_id, path)

    @_bridge
    def cookies_clear(self, profile_id: str) -> Dict[str, Any]:
        return self.ctx.cookie_service.clear(profile_id)

    # ---------------------------------------------------------------- export / import

    @_bridge
    def profiles_export(self, profile_id: str, include_credentials: bool = False) -> Dict[str, Any]:
        archive = self.ctx.archive_service
        path = self._dialog("save", save_filename=archive.default_filename(profile_id))
        if not path:
            return {"saved": False}
        return {"saved": True, **archive.export_one(profile_id, Path(path), bool(include_credentials))}

    @_bridge
    def profiles_export_many(self, profile_ids: List[str], include_credentials: bool = False) -> Dict[str, Any]:
        folder = self._dialog("folder")
        if not folder:
            return {"saved": 0, "cancelled": True}
        return self.ctx.archive_service.export_many(profile_ids, folder, bool(include_credentials))

    @_bridge
    def profiles_import(self) -> Dict[str, Any]:
        paths = self._dialog("open", allow_multiple=True, file_types=("CamouDesk profiles (*.camoudesk)", "All files (*.*)"))
        if not paths:
            return {"imported": 0, "cancelled": True}
        return self.ctx.archive_service.import_paths(paths if isinstance(paths, list) else [paths])

    @_bridge
    def profiles_import_path(self, path: str) -> Dict[str, Any]:
        return self.ctx.archive_service.import_paths([str(path)])

    # ---------------------------------------------------------------- settings

    @_bridge
    def settings_get(self) -> Dict[str, Any]:
        return self.ctx.settings_service.get()

    @_bridge
    def settings_update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self.ctx.settings_service.update(patch)  # publishes settings/theme (API server, window chrome)

    @_bridge
    def api_rotate_token(self) -> Dict[str, Any]:
        return self.ctx.settings_service.rotate_api_token()

    @_bridge
    def geoip_download(self) -> Dict[str, Any]:
        return self.ctx.settings_service.geoip_download()

    # ---------------------------------------------------------------- local REST API

    def sync_api_server(self, settings: Optional[Dict[str, Any]] = None) -> None:
        """Start/stop the REST API to match settings; a bind failure is reported, not raised."""
        from camoudesk.core import api_server

        try:
            self._api_server = api_server.sync_with_settings(self._api_server, self.ctx, settings or self.settings.get())
            self._api_error = ""
        except OSError as exc:
            self._api_server = None
            self._api_error = f"Could not listen on the port ({exc.strerror or exc})"

    def shutdown_api_server(self) -> None:
        if self._api_server is not None:
            self._api_server.stop()
            self._api_server = None

    @_bridge
    def api_status(self) -> Dict[str, Any]:
        cfg = self.settings.get()["api"]
        server = self._api_server
        return {
            "enabled": cfg["enabled"],
            "running": bool(server and server.running),
            "port": server.port if server else cfg["port"],
            "token": cfg["token"],
            "error": self._api_error,
        }
