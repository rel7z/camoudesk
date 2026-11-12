"""App settings (``settings.json``): defaults for new profiles, appearance, local API."""

import copy
from pathlib import Path
import secrets
from typing import Any, Dict, Optional

from camoudesk.config import DEFAULT_STARTUP_URL, SUPPORTED_OS, get_app_support_dir
from camoudesk.core.jsonstore import atomic_write_json, locked, read_json

DEFAULTS: Dict[str, Any] = {
    "new_profile": {
        "os": "macos",
        "timezone": "auto",
        "locale": "auto",
        "block_webrtc": True,
        "humanize": True,
        "start_urls": [DEFAULT_STARTUP_URL],
    },
    "theme": "dark",
    "api": {"enabled": False, "port": 35120, "token": ""},
}


def _merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if key not in base:
            continue  # unknown keys are ignored
        if isinstance(base[key], dict) and isinstance(value, dict):
            out[key] = _merge(base[key], value)
        else:
            out[key] = value
    return out


def _validate(settings: Dict[str, Any]) -> Dict[str, Any]:
    np = settings["new_profile"]
    if np["os"] not in SUPPORTED_OS:
        raise ValueError(f"Unsupported OS '{np['os']}'")
    for key in ("block_webrtc", "humanize"):
        np[key] = bool(np[key])
    if settings["theme"] not in ("dark", "light", "system"):
        raise ValueError("Theme must be dark, light or system")
    api = settings["api"]
    api["enabled"] = bool(api["enabled"])
    port = int(api["port"])
    if not (1024 <= port <= 65535):
        raise ValueError("API port must be between 1024 and 65535")
    api["port"] = port
    return settings


class SettingsStore:
    def __init__(self, app_dir: Optional[Path] = None) -> None:
        self.app_dir = (app_dir or get_app_support_dir()).resolve()
        self.path = self.app_dir / "settings.json"
        self.lock_path = self.app_dir / "settings.lock"

    def get(self) -> Dict[str, Any]:
        with locked(self.lock_path):
            raw = read_json(self.path, {})
        return _merge(DEFAULTS, raw if isinstance(raw, dict) else {})

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        from camoudesk.models.profile import clean_start_urls

        with locked(self.lock_path):
            raw = read_json(self.path, {})
            current = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
            merged = _validate(_merge(current, patch or {}))
            merged["new_profile"]["start_urls"] = clean_start_urls(merged["new_profile"]["start_urls"])
            if merged["api"]["enabled"] and not merged["api"]["token"]:
                merged["api"]["token"] = secrets.token_urlsafe(24)
            atomic_write_json(self.path, merged)
        return merged

    def rotate_api_token(self) -> str:
        with locked(self.lock_path):
            raw = read_json(self.path, {})
            current = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
            current["api"]["token"] = secrets.token_urlsafe(24)
            atomic_write_json(self.path, current)
        return current["api"]["token"]
