"""Address-bar search engine for profiles.

Camoufox's enterprise policy (``distribution/policies.json`` inside its app) removes every
built-in search engine and makes a placeholder engine named "None" the default. That engine
sends searches to ``127.0.0.1``, so anything typed in the address bar that is not a URL fails
like a dead connection. The policy also blocks adding engines from Firefox's own settings.

CamouDesk therefore writes the engine the user picked in Settings into the profile's
``search.json.mozlz4`` before each launch, as a user engine with a verified default, the
same record Firefox writes when a user adds an engine and makes it the default. Searches go
straight from the browser to that engine; the engine is not visible to web pages and does
not change the fingerprint.

The policy's "Remove" and "Default" actions run once per policy value, tracked by
``browser.policies.runOncePerModification.*`` prefs. On a brand-new profile "Default" would run
on first start and reset the default to "None", and Firefox 158's settings migration
(``#migrateTo14``, which has no version check) clears the "Remove" pref on every start, so
"Remove" runs at every launch. :func:`policy_prefs` presets both on Firefox's default pref
branch (where Camoufox applies launch prefs), which survives that clearing; the engine names
also stay off the Remove list so a launch without the presets only loses its first start.
"""

import base64
import hashlib
import json
from pathlib import Path
import struct
from typing import Any, Dict, Optional

from camoudesk.core.jsonstore import atomic_write_bytes

# key -> (engine name shown in the browser, search URL template). The names must not appear in
# Camoufox's policy "Remove" list ("Google", "DuckDuckGo", "Bing", ...): Firefox's settings
# migration clears that action's run-once pref on every start, so the policy deletes engines
# with those names at each launch unless the pref is also preset (see policy_prefs).
ENGINES: Dict[str, tuple] = {
    "duckduckgo": ("DuckDuckGo Search", "https://duckduckgo.com/?q={searchTerms}"),
    "google": ("Google Search", "https://www.google.com/search?q={searchTerms}"),
    "bing": ("Bing Search", "https://www.bing.com/search?q={searchTerms}"),
    "brave": ("Brave Search", "https://search.brave.com/search?q={searchTerms}"),
    "startpage": ("Startpage", "https://www.startpage.com/do/search?query={searchTerms}"),
}
NONE = "none"  # leave Camoufox's placeholder engine in place (searches go nowhere)
DEFAULT = "duckduckgo"
CHOICES = (*ENGINES, NONE)

SETTINGS_FILE = "search.json.mozlz4"
SETTINGS_VERSION = 14  # Firefox's SearchUtils.SETTINGS_VERSION; older files are migrated by Firefox
ID_PREFIX = "camoudesk-"
MOZLZ4_MAGIC = b"mozLz40\0"

# Firefox's SearchUtils.getVerificationHash salt: profile folder name + value + this text.
_DISCLAIMER = (
    "By modifying this file, I agree that I am doing so "
    "only within $appName itself, using official, user-driven search "
    "engine selection processes, and in a way which does not circumvent "
    "user consent. I acknowledge that any attempt to change this file "
    "from outside of $appName is a malicious act, and will be responded "
    "to accordingly."
)
_RUN_ONCE = "browser.policies.runOncePerModification."


# ---------------------------------------------------------------- mozlz4 (LZ4 block + header)


def lz4_block_decompress(src: bytes, size: int) -> bytes:
    """Decompress one raw LZ4 block whose decompressed size is ``size``."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i : i + lit]
        i += lit
        if i >= n:
            break  # the last sequence has literals only
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("corrupt LZ4 block")
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        for k in range(mlen):  # byte by byte: matches may overlap their own output
            out.append(out[start + k])
    if len(out) != size:
        raise ValueError("LZ4 size mismatch")
    return bytes(out)


def lz4_block_store(data: bytes) -> bytes:
    """Encode ``data`` as a valid LZ4 block made of one literal run (no compression needed)."""
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    out = bytearray([0xF0])
    rest = n - 15
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out) + data


def read_mozlz4(path: Path) -> Any:
    raw = path.read_bytes()
    if raw[:8] != MOZLZ4_MAGIC or len(raw) < 12:
        raise ValueError(f"{path.name} is not a mozlz4 file")
    (size,) = struct.unpack("<I", raw[8:12])
    return json.loads(lz4_block_decompress(raw[12:], size).decode("utf-8"))


def mozlz4_bytes(obj: Any) -> bytes:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return MOZLZ4_MAGIC + struct.pack("<I", len(data)) + lz4_block_store(data)


# ---------------------------------------------------------------- Firefox search settings


def verification_hash(value: str, profile_dir_name: str, app_name: str) -> str:
    """Firefox's SearchUtils.getVerificationHash (base64 SHA-256 of the salted value)."""
    salt = profile_dir_name + value + _DISCLAIMER.replace("$appName", app_name)
    return base64.b64encode(hashlib.sha256(salt.encode("utf-8")).digest()).decode("ascii")


def engine_record(key: str) -> Dict[str, Any]:
    name, template = ENGINES[key]
    return {
        "id": ID_PREFIX + key,
        "_name": name,
        "_loadPath": "[user]",
        "_metaData": {},
        "_urls": [{"params": [], "rels": [], "template": template}],
        "_definedAliases": [],
    }


def apply_search_engine(ff_dir: Path, choice: str, app_name: str = "Camoufox") -> bool:
    """Make ``choice`` the default search engine of the Firefox profile in ``ff_dir``.

    Call only while the profile's browser is not running. Engines CamouDesk added before are
    replaced; engines the user or Firefox added are kept. ``NONE`` removes CamouDesk's engine
    and returns the default to Camoufox's. Returns True when the file was (re)written.
    An unreadable file is left alone: Firefox resets it itself on the next start.
    """
    if choice not in CHOICES:
        raise ValueError(f"Unknown search engine '{choice}'")
    path = Path(ff_dir) / SETTINGS_FILE
    if path.exists():
        settings = read_mozlz4(path)
        if not isinstance(settings, dict) or not isinstance(settings.get("engines"), list):
            raise ValueError(f"{SETTINGS_FILE} has an unexpected layout")
    elif choice == NONE:
        return False
    else:
        settings = {"version": SETTINGS_VERSION, "engines": [], "metaData": {}}
    meta = settings.setdefault("metaData", {})
    if not isinstance(meta, dict):
        raise ValueError(f"{SETTINGS_FILE} has an unexpected layout")

    def ours(e: Any) -> bool:
        return isinstance(e, dict) and str(e.get("id", "")).startswith(ID_PREFIX)

    others = [e for e in settings["engines"] if not ours(e)]
    if choice == NONE:
        engines = others
        default_id = "" if str(meta.get("defaultEngineId", "")).startswith(ID_PREFIX) else None
    else:
        record = engine_record(choice)
        # A user engine with the same name would make Firefox skip ours.
        engines = [e for e in others if not (isinstance(e, dict) and e.get("_name") == record["_name"]
                                             and e.get("_loadPath") == "[user]")]
        engines.append(record)
        default_id = record["id"]

    new_meta = dict(meta)
    if default_id is not None:
        new_meta["defaultEngineId"] = default_id
        new_meta["defaultEngineIdHash"] = verification_hash(default_id, Path(ff_dir).name, app_name)
    if engines == settings["engines"] and new_meta == meta:
        return False
    settings["engines"] = engines
    settings["metaData"] = new_meta
    atomic_write_bytes(path, mozlz4_bytes(settings))
    return True


# ---------------------------------------------------------------- Camoufox install


def camoufox_resources_dir() -> Optional[Path]:
    """Contents/Resources of the Camoufox build that launches, or None if it isn't installed."""
    try:
        from camoufox.multiversion import get_active_path

        root = get_active_path()
    except Exception:
        return None
    if not root:
        return None
    app = Path(root) / "Camoufox.app" / "Contents" / "Resources"
    return app if app.is_dir() else Path(root)


def camoufox_app_name(resources: Optional[Path]) -> str:
    """Services.appinfo.name of the build (the [App] Name in application.ini)."""
    try:
        for line in (resources / "application.ini").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "Name" and value.strip():
                return value.strip()
    except (OSError, TypeError):
        pass
    return "Camoufox"


def policy_prefs(resources: Optional[Path]) -> Dict[str, str]:
    """Prefs that mark the SearchEngines policy's one-time actions as already applied.

    The values must equal what Firefox stores itself: the Default name, and the Remove list as
    JavaScript's JSON.stringify writes it.
    """
    try:
        policies = json.loads((resources / "distribution" / "policies.json").read_text(encoding="utf-8"))
        search = policies["policies"]["SearchEngines"]
    except (OSError, TypeError, ValueError, KeyError):
        return {}
    prefs: Dict[str, str] = {}
    if isinstance(search.get("Default"), str) and search["Default"]:
        prefs[_RUN_ONCE + "setDefaultSearchEngine"] = search["Default"]
    if isinstance(search.get("Remove"), list) and search["Remove"]:
        prefs[_RUN_ONCE + "removeSearchEngines"] = json.dumps(
            search["Remove"], ensure_ascii=False, separators=(",", ":")
        )
    return prefs


def prepare_profile_search(ff_dir: Path, choice: str) -> Dict[str, str]:
    """Write the profile's search engine and return the prefs its launch needs.

    Never blocks a launch: if anything goes wrong the browser starts with Camoufox's default.
    """
    resources = camoufox_resources_dir()
    try:
        apply_search_engine(ff_dir, choice, camoufox_app_name(resources))
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {}
    return policy_prefs(resources) if choice != NONE else {}
