"""Application paths and environment configuration for CamouDesk."""

import os
from pathlib import Path

APP_NAME = "CamouDesk"
BUNDLE_ID = "dev.camoudesk.app"
DEFAULT_STARTUP_URL = "https://browserleaks.com/canvas"
SUPPORTED_OS = ("macos", "windows", "linux")


def get_app_support_dir() -> Path:
    """Return the root data directory, honoring CAMOUDESK_HOME when set."""
    override = os.environ.get("CAMOUDESK_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / APP_NAME).resolve()


def get_profiles_dir() -> Path:
    """Return directory containing per-profile persistent browser & fingerprint folders."""
    return get_app_support_dir() / "profiles"


def get_logs_dir() -> Path:
    """Return directory containing per-profile worker stdout/stderr logs."""
    return get_app_support_dir() / "logs"


def get_registry_path() -> Path:
    """Return path to the atomic JSON profile registry file."""
    return get_app_support_dir() / "registry.json"


def get_legacy_profiles_dir() -> Path:
    """Return legacy prototype profiles path for one-time migration."""
    override = os.environ.get("CAMOUDESK_LEGACY_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".camoufox_profiles").resolve()
