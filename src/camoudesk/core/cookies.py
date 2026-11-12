"""Cookie import/export for profiles.

Formats
-------
* JSON: the common browser-extension export (list of objects with ``domain``, ``name``,
  ``value``, ``path``, ``expirationDate``, ``httpOnly``, ``secure``, ``sameSite``), Playwright
  ``storageState`` (``{"cookies": [...]}`` with ``expires``), or a bare list of either.
* Netscape ``cookies.txt``: ``domain  includeSubdomains  path  secure  expiry  name  value``
  (tab separated; ``#HttpOnly_`` prefix marks HttpOnly).

Import writes through Firefox itself (a short headless Playwright run, see browser_worker) so
Firefox owns ``cookies.sqlite``. Export reads a copy of ``cookies.sqlite`` (+WAL) so it also
works while the profile is running.
"""

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Tuple

_SAMESITE_IN = {
    "strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None",
    "unspecified": None, "": None, None: None,
}
_SAMESITE_DB = {0: "None", 1: "Lax", 2: "Strict"}


class CookieFormatError(ValueError):
    pass


def _norm_samesite(value: Any, secure: bool) -> str:
    mapped = _SAMESITE_IN.get(str(value).lower() if value is not None else None, None)
    if mapped is None or (mapped == "None" and not secure):
        return "Lax"  # browsers' default, and SameSite=None requires Secure
    return mapped


def _from_json_obj(c: Dict[str, Any]) -> Dict[str, Any]:
    name = str(c.get("name") or "")
    domain = str(c.get("domain") or c.get("host") or "").strip()
    if not name or not domain:
        raise CookieFormatError("Each cookie needs a name and a domain")
    secure = bool(c.get("secure"))
    expires = c.get("expires", c.get("expirationDate"))
    if c.get("session") or expires in (None, "", -1):
        expires = -1
    else:
        expires = float(expires)
        if expires > 1e11:  # milliseconds
            expires /= 1000.0
    host_only = c.get("hostOnly")
    if host_only is False and not domain.startswith("."):
        domain = "." + domain
    return {
        "name": name,
        "value": str(c.get("value") or ""),
        "domain": domain,
        "path": str(c.get("path") or "/"),
        "expires": expires,
        "httpOnly": bool(c.get("httpOnly")),
        "secure": secure,
        "sameSite": _norm_samesite(c.get("sameSite"), secure),
    }


def _from_netscape(text: str) -> List[Dict[str, Any]]:
    cookies = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip("\r\n")
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only, line = True, line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise CookieFormatError(f"Line {lineno}: expected 7 tab-separated fields")
        domain, include_sub, path, secure, expiry, name, value = parts
        secure_b = secure.upper() == "TRUE"
        try:
            exp = int(expiry)
        except ValueError as exc:
            raise CookieFormatError(f"Line {lineno}: expiry must be a number") from exc
        if include_sub.upper() == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        cookies.append({
            "name": name, "value": value, "domain": domain, "path": path or "/",
            "expires": float(exp) if exp > 0 else -1, "httpOnly": http_only,
            "secure": secure_b, "sameSite": "Lax",
        })
    return cookies


def parse_cookies(text: str) -> List[Dict[str, Any]]:
    """Parse JSON or Netscape cookies into Playwright ``add_cookies`` dicts."""
    stripped = (text or "").strip()
    if not stripped:
        raise CookieFormatError("No cookies found")
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CookieFormatError(f"Invalid JSON: {exc.msg} (line {exc.lineno})") from exc
        items = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise CookieFormatError("JSON must be a list of cookies or an object with a 'cookies' list")
        cookies = [_from_json_obj(c) for c in items if isinstance(c, dict)]
    else:
        cookies = _from_netscape(stripped)
    now = time.time()
    # Drop cookies that have already expired; Firefox would discard them anyway.
    live = [c for c in cookies if c["expires"] == -1 or c["expires"] > now]
    if not live:
        raise CookieFormatError("No unexpired cookies found")
    return live


def read_profile_cookies(firefox_data_dir: Path) -> List[Dict[str, Any]]:
    """Read cookies from a copy of cookies.sqlite (+WAL), safe while the browser runs."""
    src = firefox_data_dir / "cookies.sqlite"
    if not src.is_file():
        return []
    with tempfile.TemporaryDirectory(prefix="camoudesk-cookies-") as tmp:
        dst = Path(tmp) / "cookies.sqlite"
        for suffix in ("", "-wal", "-shm"):
            f = firefox_data_dir / f"cookies.sqlite{suffix}"
            if f.is_file():
                shutil.copy2(f, Path(tmp) / f.name)
        conn = sqlite3.connect(str(dst))
        try:
            rows = conn.execute(
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies"
            ).fetchall()
        finally:
            conn.close()
    out = []
    for host, name, value, path, expiry, secure, http_only, same_site in rows:
        exp = float(expiry or 0)
        if exp > 1e11:  # newer Firefox stores milliseconds
            exp /= 1000.0
        out.append({
            "domain": host, "name": name, "value": value, "path": path or "/",
            "expires": exp if exp > 0 else -1, "secure": bool(secure), "httpOnly": bool(http_only),
            "sameSite": _SAMESITE_DB.get(int(same_site or 0), "None"),
        })
    return out


def to_json(cookies: List[Dict[str, Any]]) -> str:
    """Export in the common browser-extension JSON format (importable elsewhere)."""
    items = []
    for c in cookies:
        session = c["expires"] == -1
        items.append({
            "domain": c["domain"], "hostOnly": not c["domain"].startswith("."), "name": c["name"],
            "value": c["value"], "path": c["path"], "secure": c["secure"], "httpOnly": c["httpOnly"],
            "sameSite": {"None": "no_restriction", "Lax": "lax", "Strict": "strict"}.get(c["sameSite"], "lax"),
            "session": session, **({} if session else {"expirationDate": c["expires"]}),
        })
    return json.dumps(items, indent=2)


def to_netscape(cookies: List[Dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Exported by CamouDesk", ""]
    for c in cookies:
        domain = c["domain"]
        prefix = "#HttpOnly_" if c["httpOnly"] else ""
        lines.append("\t".join([
            prefix + domain, "TRUE" if domain.startswith(".") else "FALSE", c["path"],
            "TRUE" if c["secure"] else "FALSE", str(int(c["expires"])) if c["expires"] != -1 else "0",
            c["name"], c["value"],
        ]))
    return "\n".join(lines) + "\n"


def summarize(cookies: List[Dict[str, Any]], limit: int = 50) -> Tuple[int, List[Dict[str, Any]]]:
    """Total count and per-site counts (largest first) for the UI."""
    by_site: Dict[str, int] = {}
    for c in cookies:
        site = c["domain"].lstrip(".")
        by_site[site] = by_site.get(site, 0) + 1
    ranked = sorted(by_site.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return len(cookies), [{"site": s, "count": n} for s, n in ranked]
