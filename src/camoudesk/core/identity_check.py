"""Anonymity check: grade what a profile's browser actually exposes against what it should.

``browser_worker --check-identity`` launches the profile headless through its own proxy and runs
``identity_probe.html``, which collects what a fingerprinting script reads (navigator, screen,
Intl, WebGL, canvas, audio, fonts, WebRTC candidates, the exit IP seen from inside the page), reads
it again in a Web Worker and an iframe, and checks that key getters look native. ``evaluate``
compares that with the profile's own identity and network. Detection sites mostly catch spoofing
through such inconsistencies (FP-Scanner, USENIX Security 2018), so most items are consistency
checks. Reports hold IPs and browser values, never proxy credentials.
"""

from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional

REPORT_FILENAME = "identity_check.json"
_OS_UA = {"macos": "macintosh", "windows": "windows", "linux": "linux"}
_OS_PLATFORM = {"macos": "mac", "windows": "win", "linux": "linux"}
_OS_FONTS = {  # fonts that only ship with one OS: seeing them under another identity is a tell
    "macos": {"Helvetica Neue", "Menlo", "Apple Color Emoji"},
    "windows": {"Segoe UI", "Calibri"},
    "linux": {"Ubuntu", "DejaVu Sans", "Liberation Sans"},
}
LABELS = {
    "ip": "IP address", "webrtc": "WebRTC", "timezone": "Timezone", "language": "Language",
    "user_agent": "User agent", "screen": "Screen", "cores": "CPU cores", "webdriver": "Automation flag",
    "consistency": "Worker & iframe", "native": "Native getters", "canvas": "Canvas", "webgl": "WebGL",
    "audio": "Audio", "fonts": "Fonts",
}


def _item(key: str, status: str, detail: str, hint: str = "") -> Dict[str, str]:
    return {"id": key, "label": LABELS[key], "status": status, "detail": detail, "hint": hint}


def _ua_core(ua: str) -> str:
    """A user agent without its Firefox version (Camoufox reports the running version)."""
    return re.sub(r"(rv:|Firefox/)\d+(\.\d+)?", r"\1N", ua or "")


def expected_from_claim(base: Dict[str, Any], claimed: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer what Camoufox configured for this launch over the profile's stored summary."""
    out = dict(base)
    if claimed.get("navigator.userAgent"):
        out["user_agent"] = claimed["navigator.userAgent"]
    if claimed.get("screen.width") and claimed.get("screen.height"):
        out["screen"] = f"{claimed['screen.width']}x{claimed['screen.height']}"
    if claimed.get("navigator.hardwareConcurrency"):
        out["cores"] = claimed["navigator.hardwareConcurrency"]
    if claimed.get("webGl:renderer"):
        out["gpu"] = claimed["webGl:renderer"]
    if claimed.get("timezone"):
        out["timezone"] = claimed["timezone"]
    return out


def _offset_minutes(tz: str) -> Optional[int]:
    try:
        from zoneinfo import ZoneInfo

        off = datetime.now(ZoneInfo(tz)).utcoffset()
        return int(off.total_seconds() // 60) if off is not None else None
    except Exception:
        return None


def _is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def evaluate(raw: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
    """Grade the probe's ``raw`` result. ``expected`` keys: os, user_agent, screen, cores, gpu,
    canvas_real, proxy (bool), real_ip, country, timezone, locale, webrtc (block|spoof|real),
    block_webgl."""
    items: List[Dict[str, str]] = []
    nav = raw.get("navigator") or {}
    os_ = expected.get("os") or ""
    ip_info = raw.get("ip") or {}
    exit_ip = str(ip_info.get("ip") or "")
    real_ip = str(expected.get("real_ip") or "")

    # IP: through the proxy, and never the Mac's own.
    if not exit_ip:
        items.append(_item("ip", "warn", "Could not read the exit IP from inside the browser.", "Check that the proxy works (Network tab)."))
    elif expected.get("proxy"):
        if real_ip and exit_ip == real_ip:
            items.append(_item("ip", "fail", f"Websites see this Mac's real IP ({exit_ip}).", "The proxy is not being used; check the profile's proxy."))
        else:
            where = ", ".join(x for x in (ip_info.get("city"), ip_info.get("country")) if x)
            items.append(_item("ip", "pass", f"{exit_ip}{f' ({where})' if where else ''} through the proxy."))
    else:
        items.append(_item("ip", "warn", f"No proxy: websites see this Mac's own IP ({exit_ip}).", "Assign a proxy in the Network tab to hide it."))

    # WebRTC: must not reveal another IP than the exit IP (mDNS .local host names are fine).
    rtc = raw.get("webrtc") or {}
    addrs = [c.split(" ", 1)[1] for c in rtc.get("candidates") or [] if " " in c]
    ips = [a for a in addrs if not a.endswith(".local")]
    leaked = [a for a in ips if a != exit_ip and (a == real_ip or not _is_private(a))]
    if not rtc.get("available", True) or not addrs:
        items.append(_item("webrtc", "pass", "Blocked: WebRTC reveals no address."))
    elif leaked:
        items.append(_item("webrtc", "fail", f"WebRTC reveals {', '.join(sorted(set(leaked)))}.", "Set WebRTC to Block in the Fingerprint tab."))
    elif [a for a in ips if _is_private(a)]:
        items.append(_item("webrtc", "warn", "WebRTC shows a local network address.", "Set WebRTC to Block to hide it."))
    else:
        items.append(_item("webrtc", "pass", "WebRTC shows only the proxy's address." if ips else "WebRTC shows only anonymous mDNS names."))

    # Timezone: matches the profile's setting and the exit IP's location.
    intl = raw.get("intl") or {}
    tz = str(intl.get("timeZone") or "")
    want_tz = str(expected.get("timezone") or "")
    ip_tz = str(ip_info.get("timezone") or "")
    if want_tz and tz != want_tz:
        items.append(_item("timezone", "fail", f"Browser reports {tz}, profile expects {want_tz}."))
    elif ip_tz and tz != ip_tz and _offset_minutes(tz) != _offset_minutes(ip_tz):
        if expected.get("proxy"):
            items.append(_item("timezone", "fail", f"Browser reports {tz}, but the IP is in {ip_tz}.", "Set the timezone to Auto in the Network tab."))
        else:
            items.append(_item("timezone", "warn", f"Browser reports {tz}, but the IP is in {ip_tz} (a VPN?).",
                               "Use a proxy with timezone Auto, or set the timezone to match the IP."))
    else:
        items.append(_item("timezone", "pass", f"{tz}{' matches the IP location' if ip_tz else ''}."))

    # Language: the profile's locale, and the IP's country when "auto".
    lang = str(nav.get("language") or "")
    want_locale = str(expected.get("locale") or "")
    country = str(ip_info.get("country") or expected.get("country") or "").upper()
    if want_locale and lang.lower() != want_locale.lower():
        items.append(_item("language", "warn", f"Browser language is {lang}, profile expects {want_locale}."))
    elif expected.get("proxy") and country and "-" in lang and lang.split("-")[-1].upper() != country:
        items.append(_item("language", "warn", f"Language {lang} is unusual for an IP in {country}.", "Set the language to Auto to follow the proxy's country."))
    else:
        items.append(_item("language", "pass", f"{lang or 'default'}."))

    # User agent: what the profile claims, and every navigator signal names the same OS.
    ua = str(nav.get("userAgent") or "")
    platform = str(nav.get("platform") or "").lower()
    oscpu = str(nav.get("oscpu") or "").lower()
    problems = []
    if expected.get("user_agent") and _ua_core(ua) != _ua_core(expected["user_agent"]):
        problems.append("differs from the profile's identity")
    if os_ and _OS_UA[os_] not in ua.lower():
        problems.append(f"does not name {os_}")
    if os_ and _OS_PLATFORM[os_] not in platform:
        problems.append(f"navigator.platform '{nav.get('platform')}' is not {os_}")
    if os_ and oscpu and _OS_PLATFORM[os_] not in oscpu and not (os_ == "macos" and "mac" in oscpu):
        problems.append(f"oscpu '{nav.get('oscpu')}' is not {os_}")
    items.append(_item("user_agent", "fail" if problems else "pass",
                       ("User agent " + "; ".join(problems) + ".") if problems else ua))

    scr = raw.get("screen") or {}
    got_screen = f"{scr.get('width')}x{scr.get('height')}"
    want_screen = str(expected.get("screen") or "")
    if want_screen and got_screen != want_screen:
        items.append(_item("screen", "warn", f"Screen is {got_screen}, identity says {want_screen}."))
    else:
        items.append(_item("screen", "pass", f"{got_screen} at {scr.get('devicePixelRatio')}x."))

    cores = nav.get("hardwareConcurrency")
    want_cores = expected.get("cores")
    items.append(_item("cores", "warn" if want_cores and cores != want_cores else "pass",
                       f"{cores} cores" + (f", identity says {want_cores}." if want_cores and cores != want_cores else ".")))

    items.append(_item("webdriver", "fail" if nav.get("webdriver") else "pass",
                       "navigator.webdriver is true: sites see automation." if nav.get("webdriver") else "navigator.webdriver is false."))

    # Worker and iframe must see the page's values (spoofing that misses them is a classic tell).
    mismatches = []
    worker, frame = raw.get("worker") or {}, raw.get("iframe") or {}
    for key in ("userAgent", "platform", "hardwareConcurrency"):
        if "error" not in worker and key in worker and worker[key] != nav.get(key):
            mismatches.append(f"worker {key}")
        if "error" not in frame and key in frame and frame[key] != nav.get(key):
            mismatches.append(f"iframe {key}")
    if "error" not in worker and worker.get("timeZone") and worker["timeZone"] != tz:
        mismatches.append("worker timezone")
    if "error" not in frame and frame.get("screenWidth") not in (None, scr.get("width")):
        mismatches.append("iframe screen")
    unread = [n for n, d in (("worker", worker), ("iframe", frame)) if "error" in d]
    if mismatches:
        items.append(_item("consistency", "fail", "Different values in " + ", ".join(mismatches) + "."))
    else:
        items.append(_item("consistency", "warn" if unread else "pass",
                           f"Could not read the {' and '.join(unread)}." if unread else "Worker and iframe see the same values as the page."))

    native = raw.get("native") or {}
    not_native = native.get("notNative") or []
    own = native.get("ownNavigatorProps") or []
    if not_native or own:
        what = ", ".join(not_native + [f"navigator.{p}" for p in own])
        items.append(_item("native", "fail", f"Not native: {what}. Scripts can tell these were patched."))
    else:
        items.append(_item("native", "pass", "Spoofed values come from the engine; getters look native."))

    canvas = raw.get("canvas") or {}
    if expected.get("canvas_real"):
        items.append(_item("canvas", "pass" if canvas.get("stable") else "fail",
                           "Real canvas, identical on every read (like stock Firefox)." if canvas.get("stable")
                           else "Canvas changes between reads although it should be real."))
    else:
        items.append(_item("canvas", "warn", "Covered: readings are blocked or random on each read, which some sites notice.",
                           "Only a profile matching this Mac (macOS and its GPU) can show a real canvas: use Randomize → Match this Mac."))

    gl = raw.get("webgl") or {}
    if expected.get("block_webgl"):
        items.append(_item("webgl", "pass" if not gl.get("available") else "warn",
                           "WebGL disabled as configured." if not gl.get("available") else "WebGL is on although the profile disables it."))
    elif not gl.get("available"):
        items.append(_item("webgl", "warn", "WebGL is unavailable, which is rare and can look suspicious."))
    else:
        renderer = str(gl.get("renderer") or "")
        want_gpu = str(expected.get("gpu") or "")
        bad = want_gpu and renderer != want_gpu
        items.append(_item("webgl", "warn" if bad else "pass",
                           f"{renderer}" + (f" (identity says {want_gpu})" if bad else "")))

    audio = raw.get("audio") or {}
    items.append(_item("audio", "pass" if audio.get("stable") else "warn",
                       "Audio fingerprint is stable." if audio.get("stable") else "Audio output changes between runs, which can mark noise."))

    fonts = raw.get("fonts") or {}
    present = {f for f, ok in fonts.items() if ok}
    foreign = sorted(f for other, names in _OS_FONTS.items() if other != os_ for f in names & present)
    items.append(_item("fonts", "warn" if foreign else "pass",
                       f"Fonts from another system are visible: {', '.join(foreign)}." if foreign else "Fonts fit the claimed system."))

    statuses = [i["status"] for i in items]
    overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    return {
        "status": overall,
        "passed": statuses.count("pass"),
        "total": len(items),
        "items": items,
        "exit_ip": exit_ip,
        "checked_at": time.strftime("%Y-%m-%d %H:%M"),
        "errors": [re.sub(r"https?://\S+", "<url>", e)[:200] for e in raw.get("errors") or []][:10],
    }


def save_report(profile_dir: Path, report: Dict[str, Any]) -> None:
    from camoudesk.core.jsonstore import atomic_write_json

    atomic_write_json(profile_dir / REPORT_FILENAME, report)


def load_report(profile_dir: Path) -> Optional[Dict[str, Any]]:
    path = profile_dir / REPORT_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "items" in data else None
