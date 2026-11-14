"""Deterministic per-profile hardware fingerprint generation and persistence."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional, Tuple

from camoufox.fingerprints import generate_fingerprint

from camoudesk.models.profile import FingerprintSummary, validate_os_target

FINGERPRINT_FILENAME = "fingerprint.json"
_GENERATE_ATTEMPTS = 12
# A saved fingerprint that is one of Camoufox's recorded real devices (fingerprint-presets*.json)
# instead of an fpgen sample. Launches pass it as fingerprint_preset=<dict> (see launcher).
PRESET_MARK = "_camoudesk_source"
SOURCES = ("generated", "preset")


def is_preset(fp: Any) -> bool:
    return isinstance(fp, dict) and fp.get(PRESET_MARK) == "preset"


def preset_payload(fp: Dict[str, Any]) -> Dict[str, Any]:
    """The preset as Camoufox expects it (without CamouDesk's marker)."""
    return {k: v for k, v in fp.items() if k != PRESET_MARK}


def _browser_major() -> str:
    try:
        from camoufox.utils import resolve_verstr

        return str(resolve_verstr(None)).split(".", 1)[0]
    except Exception:
        return ""


def generate_preset_fingerprint(
    os_target: str,
    screen: Optional[Tuple[int, int]] = None,
    gpu_renderer: Optional[str] = None,
) -> Dict[str, Any]:
    """A random recorded real device for ``os_target`` (optionally with this screen or GPU)."""
    import random

    from camoufox.fingerprints import load_presets

    normalized_os = validate_os_target(os_target)
    data = load_presets(_browser_major() or None) or {}
    candidates = [p for p in (data.get("presets") or {}).get(normalized_os, []) if fingerprint_matches_os(p, normalized_os)]
    if screen:
        candidates = [p for p in candidates if ((p.get("screen") or {}).get("width"), (p.get("screen") or {}).get("height")) == tuple(screen)]
    if gpu_renderer:
        candidates = [p for p in candidates if (p.get("webgl") or {}).get("unmaskedRenderer") == gpu_renderer]
    if not candidates:
        what = " with a {}x{} screen".format(*screen) if screen else ""
        raise ValueError(f"No recorded real {normalized_os} device{what} is available; use a generated fingerprint.")
    chosen = json.loads(json.dumps(random.choice(candidates)))
    chosen[PRESET_MARK] = "preset"
    return chosen


def summarize(os_target: str, fp: Dict[str, Any]) -> FingerprintSummary:
    """Summary of what websites will see; a preset's user agent is the running Firefox's version."""
    summary = FingerprintSummary.from_fpgen_dict(validate_os_target(os_target), fp)
    if is_preset(fp):
        try:
            from camoufox.fingerprints import from_preset

            summary.user_agent = str(from_preset(preset_payload(fp), _browser_major() or None).get("navigator.userAgent") or summary.user_agent)
        except Exception:
            pass
    return summary

# Every navigator signal must name the same OS: (platform, oscpu, userAgent) markers.
_OS_MARKERS = {
    "macos": ("mac", "mac os x", "macintosh"),
    "windows": ("win", "windows", "windows"),
    "linux": ("linux", "linux", "linux"),
}


def fingerprint_matches_os(fp: Dict[str, Any], os_target: str) -> bool:
    """True when navigator.platform, oscpu and userAgent all agree with ``os_target``."""
    nav = fp.get("navigator") or {}
    platform_m, oscpu_m, ua_m = _OS_MARKERS[os_target]
    platform = str(nav.get("platform") or "").lower()
    oscpu = str(nav.get("oscpu") or "").lower()
    ua = str(nav.get("userAgent") or "").lower()
    if platform_m not in platform or ua_m not in ua:
        return False
    return not oscpu or oscpu_m in oscpu


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
        with open(fp_path, encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict) and "navigator" in data and "screen" in data:
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def generate_verified_fingerprint(
    os_target: str,
    screen: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """Generate a coherent Firefox fingerprint via fpgen without saving it (e.g. for a preview).

    ``screen`` pins the generated screen to an exact (width, height); a size no recorded device
    of that OS has raises ValueError.
    """
    normalized_os = validate_os_target(os_target)
    kwargs: Dict[str, Any] = {"os": normalized_os}
    if screen:
        from camoufox.fingerprints import Screen

        w, h = screen
        kwargs["screen"] = Screen(min_width=w, max_width=w, min_height=h, max_height=h)
    # fpgen's output is verified rather than trusted: it ignored an exact screen bound about 1 in
    # 8 times (2560x1440 -> 3440x1440 on Windows), and ~3% of "linux" fingerprints were Windows
    # or mixed (Linux platform with a Windows oscpu) -- an incoherence detection scripts look for.
    for _ in range(_GENERATE_ATTEMPTS):
        try:
            candidate = generate_fingerprint(**kwargs)
        except Exception as exc:
            if screen:
                raise ValueError(f"No realistic {normalized_os} device has a {screen[0]}x{screen[1]} screen; pick another size.") from exc
            raise
        if not fingerprint_matches_os(candidate, normalized_os):
            continue
        scr = candidate.get("screen") or {}
        if screen and (scr.get("width"), scr.get("height")) != tuple(screen):
            continue
        # Round-trip via JSON so stored and in-memory structures match identically.
        return json.loads(json.dumps(candidate))
    size = f" with a {screen[0]}x{screen[1]} screen" if screen else ""
    raise ValueError(f"Could not generate a consistent {normalized_os} fingerprint{size}; try again or pick another size.")


def save_fingerprint(profile_dir: Path, os_target: str, fp: Dict[str, Any]) -> FingerprintSummary:
    """Persist an already generated fingerprint after re-checking it matches ``os_target``."""
    normalized_os = validate_os_target(os_target)
    if not isinstance(fp, dict) or not fingerprint_matches_os(fp, normalized_os):
        raise ValueError(f"That fingerprint is not a consistent {normalized_os} device")
    serializable = json.loads(json.dumps(fp))
    _atomic_write_json(profile_dir / FINGERPRINT_FILENAME, serializable)
    return summarize(normalized_os, serializable)


def generate_and_save_fingerprint(
    profile_dir: Path,
    os_target: str,
    screen: Optional[Tuple[int, int]] = None,
    source: str = "generated",
) -> Tuple[Dict[str, Any], FingerprintSummary]:
    """Generate a coherent fingerprint (fpgen, or a recorded real device) and persist it.

    On failure (e.g. an impossible screen size) the saved fingerprint is left untouched.
    """
    fp = generate_preset_fingerprint(os_target, screen) if source == "preset" else generate_verified_fingerprint(os_target, screen)
    return fp, save_fingerprint(profile_dir, os_target, fp)


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
            summary = summarize(normalized_os, saved)
            return saved, summary

    return generate_and_save_fingerprint(profile_dir, normalized_os)


def summary_dict(summary: FingerprintSummary) -> Dict[str, Any]:
    """Convert FingerprintSummary into JSON-safe dict for Profile registry storage."""
    return asdict(summary)
