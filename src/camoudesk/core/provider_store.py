"""Provider API keys (``providers.json``, 0600) and cached targeting lists (``providers_cache.json``).

Keys are secrets: they are never returned to the UI, written to logs, or included in exports.
"""

import os
from pathlib import Path
import time
from typing import Any, Dict, Optional

from camoudesk.config import get_app_support_dir
from camoudesk.core.jsonstore import atomic_write_json, locked, read_json

PROVIDERS = ("evomi",)
CACHE_TTL = 24 * 3600


class ProviderStore:
    def __init__(self, app_dir: Optional[Path] = None) -> None:
        self.app_dir = (app_dir or get_app_support_dir()).resolve()
        self.path = self.app_dir / "providers.json"
        self.cache_path = self.app_dir / "providers_cache.json"
        self.lock_path = self.app_dir / "providers.lock"

    @staticmethod
    def _check(name: str) -> None:
        if name not in PROVIDERS:
            raise ValueError(f"Unknown provider '{name}'")

    def _read(self) -> Dict[str, Any]:
        raw = read_json(self.path, {})
        return raw if isinstance(raw, dict) else {}

    def api_key(self, name: str) -> str:
        self._check(name)
        with locked(self.lock_path):
            entry = self._read().get(name)
        return str(entry.get("api_key") or "") if isinstance(entry, dict) else ""

    def set_api_key(self, name: str, key: str) -> None:
        self._check(name)
        key = (key or "").strip()
        if not key or len(key) > 200 or any(c.isspace() for c in key):
            raise ValueError("Enter a valid API key")
        with locked(self.lock_path):
            data = self._read()
            data[name] = {"api_key": key, "saved_at": time.strftime("%Y-%m-%d %H:%M")}
            atomic_write_json(self.path, data)
            os.chmod(self.path, 0o600)

    def clear_api_key(self, name: str) -> None:
        self._check(name)
        with locked(self.lock_path):
            data = self._read()
            if data.pop(name, None) is not None:
                atomic_write_json(self.path, data)
                os.chmod(self.path, 0o600)
            cache = read_json(self.cache_path, {})
            if isinstance(cache, dict) and any(k.startswith(f"{name}:") for k in cache):
                atomic_write_json(self.cache_path, {k: v for k, v in cache.items() if not k.startswith(f"{name}:")})

    def status(self) -> Dict[str, Any]:
        """Public view: whether a key is set, never the key itself."""
        with locked(self.lock_path):
            data = self._read()
        out: Dict[str, Any] = {}
        for name in PROVIDERS:
            entry = data.get(name) if isinstance(data.get(name), dict) else {}
            out[name] = {"has_key": bool(entry.get("api_key")), "saved_at": str(entry.get("saved_at") or "")}
        return out

    def cached_targets(self, name: str, product: str, max_age: float = CACHE_TTL) -> Optional[Dict[str, Any]]:
        with locked(self.lock_path):
            cache = read_json(self.cache_path, {})
        entry = cache.get(f"{name}:{product}") if isinstance(cache, dict) else None
        if not isinstance(entry, dict) or time.time() - float(entry.get("ts") or 0) > max_age:
            return None
        return entry.get("targets") if isinstance(entry.get("targets"), dict) else None

    def store_targets(self, name: str, product: str, targets: Dict[str, Any]) -> None:
        with locked(self.lock_path):
            cache = read_json(self.cache_path, {})
            cache = cache if isinstance(cache, dict) else {}
            cache[f"{name}:{product}"] = {"ts": time.time(), "targets": targets}
            atomic_write_json(self.cache_path, cache)
