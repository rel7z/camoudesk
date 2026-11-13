"""Saved-proxy registry (``proxies.json``) with the same safety rules as the profile registry."""

import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from camoudesk.config import get_app_support_dir
from camoudesk.core.jsonstore import atomic_write_json, locked, read_json
from camoudesk.core.proxy_parse import ParseError, parse_proxy_list
from camoudesk.models.proxy import ProxyCheck, SavedProxy

_Entry = Union[SavedProxy, Dict[str, Any]]


class ProxyStore:
    def __init__(self, app_dir: Optional[Path] = None) -> None:
        self.app_dir = (app_dir or get_app_support_dir()).resolve()
        self.path = self.app_dir / "proxies.json"
        self.lock_path = self.app_dir / "proxies.lock"

    # Invalid entries are kept as raw dicts and written back untouched (never silently erased).
    def _read(self) -> List[_Entry]:
        raw = read_json(self.path, [])
        if not isinstance(raw, list):
            raise ValueError(f"{self.path} is not a JSON list; CamouDesk will not overwrite it.")
        entries: List[_Entry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(SavedProxy.from_dict(item))
            except (ValueError, KeyError, TypeError):
                entries.append(item)
        return entries

    def _write(self, entries: List[_Entry]) -> None:
        atomic_write_json(self.path, [e.to_dict() if isinstance(e, SavedProxy) else e for e in entries])
        try:
            os.chmod(self.path, 0o600)  # contains proxy passwords
        except OSError:
            pass

    @staticmethod
    def _find(entries: List[_Entry], proxy_id: str) -> Optional[SavedProxy]:
        return next((e for e in entries if isinstance(e, SavedProxy) and e.id == proxy_id), None)

    @staticmethod
    def _new_id() -> str:
        return f"px_{int(time.time())}_{os.urandom(3).hex()}"

    @staticmethod
    def _dedupe_key(proxy: SavedProxy) -> Tuple[Any, ...]:
        # One provider account can be saved once per targeting (e.g. "Evomi US" and "Evomi DE").
        target = tuple(proxy.provider.get(k, "") for k in ("country", "region", "city", "mode")) if proxy.provider else ()
        return (proxy.type, proxy.host, proxy.port, proxy.username, *target)

    def list(self) -> List[SavedProxy]:
        with locked(self.lock_path):
            return [e for e in self._read() if isinstance(e, SavedProxy)]

    def get(self, proxy_id: str) -> Optional[SavedProxy]:
        if not proxy_id:
            return None
        with locked(self.lock_path):
            return self._find(self._read(), proxy_id)

    def create(self, data: Dict[str, Any]) -> SavedProxy:
        proxy = SavedProxy.from_dict({**data, "id": self._new_id(), "last_check": None})
        with locked(self.lock_path):
            entries = self._read()
            entries.append(proxy)
            self._write(entries)
        return proxy

    def update(self, proxy_id: str, data: Dict[str, Any]) -> SavedProxy:
        with locked(self.lock_path):
            entries = self._read()
            current = self._find(entries, proxy_id)
            if current is None:
                raise KeyError(f"Proxy '{proxy_id}' not found")
            merged = current.to_dict()
            for key in ("name", "type", "host", "port", "username", "change_ip_url", "notes", "tags", "provider"):
                if key in data:
                    merged[key] = data[key]
            # An empty password from the UI means "unchanged" (the UI never receives passwords).
            if data.get("password"):
                merged["password"] = data["password"]
            if data.get("clear_password"):
                merged["password"] = ""
            updated = SavedProxy.from_dict(merged)
            connection_changed = any(getattr(updated, k) != getattr(current, k)
                                     for k in ("type", "host", "port", "username", "password", "provider"))
            if connection_changed:
                updated.last_check = ProxyCheck()  # results belong to the old endpoint
            entries[entries.index(current)] = updated
            self._write(entries)
            return updated

    def delete(self, proxy_id: str) -> bool:
        with locked(self.lock_path):
            entries = self._read()
            remaining = [e for e in entries if not (isinstance(e, SavedProxy) and e.id == proxy_id)]
            if len(remaining) == len(entries):
                return False
            self._write(remaining)
            return True

    def record_check(self, proxy_id: str, result: Dict[str, Any]) -> Optional[SavedProxy]:
        with locked(self.lock_path):
            entries = self._read()
            current = self._find(entries, proxy_id)
            if current is None:
                return None
            current.last_check = ProxyCheck.from_result(result)
            self._write(entries)
            return current

    def import_text(self, text: str, default_scheme: str = "http", name_prefix: str = "") -> Tuple[List[SavedProxy], List[ParseError]]:
        configs, errors = parse_proxy_list(text, default_scheme)
        created: List[SavedProxy] = []
        with locked(self.lock_path):
            entries = self._read()
            existing = {self._dedupe_key(e) for e in entries if isinstance(e, SavedProxy)}
            for i, cfg in enumerate(configs, start=1):
                proxy = SavedProxy.from_config(
                    self._new_id() + f"{i:03d}"[-3:], f"{name_prefix}{cfg.host}:{cfg.port}", cfg
                )
                if proxy.provider:
                    proxy.name = f"{name_prefix}Evomi " + " ".join(
                        filter(None, [proxy.provider.get("country"), proxy.provider.get("city")])) or proxy.name
                    proxy.name = proxy.name.strip()
                key = self._dedupe_key(proxy)
                if key in existing:
                    continue  # skip duplicates of already-saved proxies
                existing.add(key)
                entries.append(proxy)
                created.append(proxy)
            if created:
                self._write(entries)
        return created, errors
