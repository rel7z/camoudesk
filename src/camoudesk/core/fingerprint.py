"""Deterministic per-profile hardware fingerprint generation and persistence."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Tuple

from camoufox.fingerprints import generate_fingerprint

from camoudesk.models.profile import FingerprintSummary, validate_os_target

FINGERPRINT_FILENAME = "fingerprint.json"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically write JSON payload to path within the same parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".fp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def load_saved_fingerprint(profile_dir: Path) -> Dict[str, Any] | None:
    """Load saved fingerprint JSON from profile directory if present and valid."""
    fp_path = profile_dir / FINGERPRINT_FILENAME
    if not fp_path.is_file():
        return None
    try:
        with open(fp_path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict) and "navigator" in data and "screen" in data:
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def generate_and_save_fingerprint(
    profile_dir: Path,
    os_target: str,
) -> Tuple[Dict[str, Any], FingerprintSummary]:
    """Generate a coherent Firefox fingerprint via fpgen and persist it to disk."""
    normalized_os = validate_os_target(os_target)
    raw_fp = generate_fingerprint(os=normalized_os)
    # Round-trip via JSON so stored and in-memory structures match identically.
    serializable = json.loads(json.dumps(raw_fp))
    _atomic_write_json(profile_dir / FINGERPRINT_FILENAME, serializable)
    summary = FingerprintSummary.from_fpgen_dict(normalized_os, serializable)
    return serializable, summary


def ensure_profile_fingerprint(
    profile_dir: Path,
    os_target: str,
) -> Tuple[Dict[str, Any], FingerprintSummary]:
    """Return existing frozen fingerprint if OS matches; otherwise generate a new one."""
    normalized_os = validate_os_target(os_target)
    saved = load_saved_fingerprint(profile_dir)
    if saved is not None:
        # fpgen records target platform inside `os` (e.g. 'mac', 'win', 'linux') or navigator.platform
        platform_str = str((saved.get("navigator") or {}).get("platform") or "").lower()
        os_matches = (
            (normalized_os == "macos" and "mac" in platform_str)
            or (normalized_os == "windows" and "win" in platform_str)
            or (normalized_os == "linux" and "linux" in platform_str)
        )
        if os_matches:
            summary = FingerprintSummary.from_fpgen_dict(normalized_os, saved)
            return saved, summary

    return generate_and_save_fingerprint(profile_dir, normalized_os)


def summary_dict(summary: FingerprintSummary) -> Dict[str, Any]:
    """Convert FingerprintSummary into JSON-safe dict for Profile registry storage."""
    return asdict(summary)
