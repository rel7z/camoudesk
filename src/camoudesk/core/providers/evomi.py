"""Evomi residential proxies: targeting and sessions encoded in the proxy password.

Evomi reads options appended to the password, e.g.
``base_country-US_city-new.york_session-k3x9q2m7ab_lifetime-1440``
(https://docs.evomi.com/proxy-instructions/residential-proxies/proxy-sessions/):

* ``session-<id>``: sticky IP for ``lifetime-<minutes>`` (1–1440, Evomi's default 30)
* ``hardsession-<id>``: keep the IP as long as possible (no lifetime)
* ``lockedsession-<id>``: never change the IP; requests fail if it goes offline (no lifetime)
* no session: a new IP for every request ("rotating")

A saved Evomi proxy stores only the account's base password plus default targeting. Each profile
then gets its own session ID, derived from the profile id and a rotation counter, so profiles
sharing one Evomi account get different, stable IPs, and every launch path (direct, worker, SDK)
computes the same credentials without writing anything. "New IP" bumps the counter.
"""

import hashlib
import re
import time
from typing import Any, Dict, List, Optional, Tuple

NAME = "evomi"
PRODUCTS = {"rpc": "Core Residential", "rp": "Premium Residential", "mp": "Mobile"}
MODES = ("sticky", "hard", "locked", "rotating")
MAX_LIFETIME = 1440
DEFAULT_LIFETIME = 1440  # browser profiles want the IP stable for the whole day
API_BASE = "https://api.evomi.com/public"

_TARGET_KEYS = ("country", "region", "city", "continent", "isp")
_SESSION_KEYS = {"session": "sticky", "hardsession": "hard", "lockedsession": "locked"}
_KNOWN = set(_TARGET_KEYS) | set(_SESSION_KEYS) | {"lifetime"}
# Options Evomi documents beyond targeting/sessions; kept verbatim when present in a pasted password.
_EXTRA_KEYS = {"asn", "zip", "device", "fraudscore", "latency", "activesince", "geosource", "cache",
               "adblock", "extended", "maxtimeout"}
_OPTION_RE = re.compile(r"_([a-z]+)-([A-Za-z0-9.\-]+)")
_PLACE_RE = re.compile(r"^[a-z0-9.\-]{1,60}$")


def detect(host: str) -> Optional[str]:
    """The Evomi product for a gateway host (``rpc``, ``rp``, ``mp``), or None if not Evomi."""
    h = (host or "").strip().lower().rstrip(".")
    if not (h.endswith(".evomi.com") or h.endswith(".evomi-proxy.com")):
        return None
    if h.startswith("core-residential.") or h.startswith("rpc."):
        return "rpc"
    if h.startswith("mp.") or h.startswith("mobile."):
        return "mp"
    return "rp"


def split_password(password: str) -> Tuple[str, Dict[str, str]]:
    """Split ``base_country-US_session-abc`` into the base password and its known options.

    Options are only peeled off the end of the password, and only for names Evomi documents, so a
    base password that itself contains ``_`` or ``-`` is left intact.
    """
    base, params = password, {}
    while True:
        cut = None
        for m in _OPTION_RE.finditer(base):
            if m.end() == len(base) and (m.group(1) in _KNOWN or m.group(1) in _EXTRA_KEYS):
                cut = m
        if cut is None:
            break
        params.setdefault(cut.group(1), cut.group(2))
        base = base[: cut.start()]
    return base, params


def place(value: Any) -> str:
    """Evomi's region/city spelling: lowercase, spaces as dots ("New York" -> "new.york")."""
    text = re.sub(r"\s+", ".", str(value or "").strip().lower())
    return text if _PLACE_RE.fullmatch(text) else ""


def clean_opts(raw: Any, *, partial: bool = False) -> Dict[str, Any]:
    """Validate provider options; ``partial`` keeps only what is set (per-profile overrides)."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {}
    cc = str(raw.get("country") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", cc):
        out["country"] = cc
    for key in ("region", "city"):
        val = place(raw.get(key))
        if val:
            out[key] = val
    cont = place(raw.get("continent"))
    if cont:
        out["continent"] = cont
    isp = str(raw.get("isp") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9.\-]{1,60}", isp):
        out["isp"] = isp
    mode = str(raw.get("mode") or "").strip().lower()
    if mode in MODES:
        out["mode"] = mode
    elif not partial:
        out["mode"] = "sticky"
    try:
        lifetime = int(raw.get("lifetime") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    if lifetime:
        if not (1 <= lifetime <= MAX_LIFETIME):
            raise ValueError(f"Evomi session lifetime must be 1–{MAX_LIFETIME} minutes")
        out["lifetime"] = lifetime
    elif not partial:
        out["lifetime"] = DEFAULT_LIFETIME
    if not partial:
        product = str(raw.get("product") or "").strip().lower()
        out["product"] = product if product in PRODUCTS else "rp"
        extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        out["extra"] = {k: str(v) for k, v in extra.items()
                        if k in _EXTRA_KEYS and re.fullmatch(r"[A-Za-z0-9.\-]{1,40}", str(v))}
        out["name"] = NAME
    return out


def opts_from_params(params: Dict[str, str], product: str) -> Dict[str, Any]:
    """Provider options from a pasted password's options (its session ID is dropped)."""
    mode = next((_SESSION_KEYS[k] for k in _SESSION_KEYS if k in params), "rotating")
    raw: Dict[str, Any] = {k: params[k] for k in _TARGET_KEYS if k in params}
    raw.update(mode=mode, product=product, extra={k: v for k, v in params.items() if k in _EXTRA_KEYS})
    if mode == "sticky" and params.get("lifetime", "").isdigit():
        raw["lifetime"] = min(max(int(params["lifetime"]), 1), MAX_LIFETIME)
    return clean_opts(raw)


def effective_opts(proxy_opts: Dict[str, Any], profile_opts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The proxy's defaults with the profile's overrides on top.

    Changing the country drops the proxy's region/city unless the profile sets its own (a city
    only makes sense inside its country).
    """
    merged = dict(proxy_opts)
    over = clean_opts(profile_opts, partial=True) if profile_opts else {}
    if over.get("country") and over["country"] != proxy_opts.get("country"):
        merged.pop("region", None)
        merged.pop("city", None)
    merged.update({k: v for k, v in over.items() if k in _TARGET_KEYS or k in ("mode", "lifetime")})
    return merged


def session_id(profile_id: str, rotation: int = 0) -> str:
    """Stable 10-character alphanumeric session ID for a profile (Evomi accepts 6–10)."""
    digest = hashlib.sha256(f"camoudesk:evomi:{profile_id}:{int(rotation)}".encode()).digest()
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    num = int.from_bytes(digest[:10], "big")
    out = []
    for _ in range(10):
        num, rem = divmod(num, len(alphabet))
        out.append(alphabet[rem])
    return "".join(out)


def compose_password(base: str, opts: Dict[str, Any], session: str = "") -> str:
    """Base password plus targeting, then the session option, then lifetime (Evomi's order)."""
    parts = [base]
    for key in _TARGET_KEYS:
        if opts.get(key):
            parts.append(f"{key}-{opts[key]}")
    for key, val in sorted((opts.get("extra") or {}).items()):
        parts.append(f"{key}-{val}")
    mode = opts.get("mode") or "sticky"
    if mode != "rotating" and session:
        key = {"sticky": "session", "hard": "hardsession", "locked": "lockedsession"}[mode]
        parts.append(f"{key}-{session}")
        if mode == "sticky":
            parts.append(f"lifetime-{int(opts.get('lifetime') or DEFAULT_LIFETIME)}")
    return "_".join(parts)


def summary(opts: Dict[str, Any]) -> str:
    """Short human label, e.g. "Core Residential · US · new.york · Sticky 24h"."""
    bits = [PRODUCTS.get(opts.get("product", ""), "Evomi")]
    bits += [opts[k] for k in ("country", "region", "city") if opts.get(k)]
    mode = opts.get("mode") or "sticky"
    if mode == "sticky":
        minutes = int(opts.get("lifetime") or DEFAULT_LIFETIME)
        bits.append(f"Sticky {minutes // 60}h" if minutes % 60 == 0 else f"Sticky {minutes}m")
    else:
        bits.append({"hard": "Hard session", "locked": "Locked IP", "rotating": "Rotating"}[mode])
    return " · ".join(bits)


# ---------------------------------------------------------------------- public API (targeting lists)


class EvomiApiError(RuntimeError):
    pass


def fetch_targets(api_key: str, product: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Countries/regions/cities Evomi offers for ``product`` (GET /public/settings, x-apikey)."""
    import requests

    try:
        resp = requests.get(f"{API_BASE}/settings", headers={"x-apikey": api_key, "User-Agent": "CamouDesk"},
                            timeout=timeout)
    except requests.RequestException as exc:
        raise EvomiApiError(f"Could not reach Evomi: {exc.__class__.__name__}") from exc
    if resp.status_code in (401, 403):
        raise EvomiApiError("Evomi rejected the API key")
    if resp.status_code == 429:
        raise EvomiApiError("Evomi rate limit reached; try again in a minute")
    if resp.status_code != 200:
        raise EvomiApiError(f"Evomi API returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise EvomiApiError("Evomi API returned an unreadable response") from exc
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    block = data.get(product) or data.get("rp") or {}
    return normalize_targets(block)


def _names(value: Any) -> List[str]:
    """Accept lists of strings or objects, or {code: name} maps, as Evomi's examples vary."""
    items: List[str] = []
    if isinstance(value, dict):
        value = list(value.keys())
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            item = item.get("code") or item.get("value") or item.get("name") or ""
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def normalize_targets(block: Any) -> Dict[str, Any]:
    """{countries: [ISO], regions: {CC: [..]}, cities: {CC: [..]}} from an Evomi settings block."""
    block = block if isinstance(block, dict) else {}
    countries = sorted({c.upper() for c in _names(block.get("countries")) if re.fullmatch(r"[A-Za-z]{2}", c)})

    def per_country(value: Any) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        if isinstance(value, dict):
            for cc, names in value.items():
                if re.fullmatch(r"[A-Za-z]{2}", str(cc)):
                    out[str(cc).upper()] = sorted({place(n) for n in _names(names) if place(n)})
        elif isinstance(value, list):  # [{country: "US", name: "new.york"}, ...]
            for item in value:
                if isinstance(item, dict):
                    cc = str(item.get("country") or item.get("countryCode") or "").upper()
                    name = place(item.get("name") or item.get("value") or item.get("code"))
                    if re.fullmatch(r"[A-Z]{2}", cc) and name:
                        out.setdefault(cc, []).append(name)
            out = {k: sorted(set(v)) for k, v in out.items()}
        return out

    return {"countries": countries, "regions": per_country(block.get("regions")),
            "cities": per_country(block.get("cities")), "fetched_at": time.strftime("%Y-%m-%d %H:%M")}
