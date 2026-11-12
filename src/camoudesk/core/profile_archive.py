"""Portable profile archives (``.camoudesk`` zip files) for moving profiles between Macs.

Layout::

    manifest.json        format/version, profile settings, optional saved-proxy definition
    fingerprint.json     the frozen identity (same device on the new Mac)
    firefox_data/...     cookies, storage, history (caches and lock files excluded)

Proxy passwords are excluded unless explicitly requested. Imports are treated as untrusted:
member paths are confined to the expected layout (no absolute paths, ``..`` or symlinks), and
the number and total size of members are capped.
"""

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
from typing import Any, Dict, Optional
import zipfile

from camoudesk import __version__
from camoudesk.core.fingerprint import FINGERPRINT_FILENAME, load_saved_fingerprint
from camoudesk.models.profile import Profile, ProxyConfig, validate_os_target

FORMAT = "camoudesk-profile"
VERSION = 1
SUFFIX = ".camoudesk"
MAX_MEMBERS = 50_000
MAX_TOTAL_BYTES = 4 * 1024 ** 3  # 4 GB uncompressed
_SKIP_DIRS = {"cache2", "startupCache", "crashes", "minidumps", "saved-telemetry-pings", "datareporting", "shader-cache"}
_SKIP_FILES = {"lock", ".parentlock", "parent.lock", "run.json", ".cookie-import.json"}


class ArchiveError(ValueError):
    pass


def _strip_credentials(url: str) -> str:
    if not url:
        return ""
    cfg = ProxyConfig.from_url(url)
    host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host
    user = f"{cfg.username}@" if cfg.username else ""
    return f"{cfg.scheme}://{user}{host}:{cfg.port}"


def export_profile(store: Any, proxies: Any, profile_id: str, dest: Path, include_credentials: bool = False) -> Dict[str, Any]:
    profile = store.get_profile(profile_id)
    if profile is None:
        raise KeyError("Profile not found")
    data = profile.to_dict()
    for key in ("id", "deleted_at", "last_used_at"):
        data.pop(key, None)
    if not include_credentials:
        data["proxy"] = _strip_credentials(data.get("proxy") or "")
    manifest: Dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "app_version": __version__,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "includes_credentials": bool(include_credentials),
        "profile": data,
    }
    if profile.proxy_id and proxies is not None:
        saved = proxies.get(profile.proxy_id)
        if saved is not None:
            sp = saved.to_dict()
            for key in ("id", "last_check", "created_at"):
                sp.pop(key, None)
            if not include_credentials:
                sp["password"] = ""
            manifest["saved_proxy"] = sp

    pdir = store.profile_dir(profile.id)
    ff_dir = store.firefox_data_dir(profile.id)
    dest = Path(dest)
    tmp = dest.with_name(f".{dest.name}.partial")
    files = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            fp_path = pdir / FINGERPRINT_FILENAME
            if fp_path.is_file():
                zf.write(fp_path, FINGERPRINT_FILENAME)
            if ff_dir.is_dir():
                for root, dirs, names in os.walk(ff_dir):
                    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not os.path.islink(os.path.join(root, d))]
                    for name in names:
                        full = Path(root) / name
                        if name in _SKIP_FILES or full.is_symlink() or not full.is_file():
                            continue
                        zf.write(full, "firefox_data/" + full.relative_to(ff_dir).as_posix())
                        files += 1
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"path": str(dest), "files": files, "bytes": dest.stat().st_size}


def _check_member(info: zipfile.ZipInfo) -> Optional[str]:
    """Validated relative path for a member, or None for directory entries."""
    name = info.filename
    if name.endswith("/"):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or name.startswith("/"):
        raise ArchiveError(f"Unsafe path in archive: {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ArchiveError(f"Symbolic links are not allowed in archives: {name!r}")
    if name not in ("manifest.json", FINGERPRINT_FILENAME) and path.parts[0] != "firefox_data":
        raise ArchiveError(f"Unexpected file in archive: {name!r}")
    return name


def read_manifest(src: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(src) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as exc:
        raise ArchiveError("Not a CamouDesk profile file") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ArchiveError("Not a CamouDesk profile file")
    if int(manifest.get("version") or 0) > VERSION:
        raise ArchiveError("This profile was exported by a newer CamouDesk; please update first")
    if not isinstance(manifest.get("profile"), dict):
        raise ArchiveError("Profile file is missing its settings")
    return manifest


def import_profile(store: Any, proxies: Any, src: Path, name: str = "") -> Profile:
    src = Path(src)
    manifest = read_manifest(src)
    raw = dict(manifest["profile"])
    validate_os_target(str(raw.get("os") or "macos"))
    new_id = f"prof_{int(time.time())}_{os.urandom(2).hex()}"
    dest = store.profile_dir(new_id)
    if dest.exists():
        raise ArchiveError("Target profile folder already exists")

    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError("Archive has too many files")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ArchiveError("Archive is too large")
        members = [(i, _check_member(i)) for i in infos]
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=str(store.profiles_dir)))
        try:
            root = staging.resolve()
            for info, rel in members:
                if rel is None or rel == "manifest.json":
                    continue
                target = (staging / rel).resolve()
                if root not in target.parents:
                    raise ArchiveError(f"Unsafe path in archive: {rel!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fin, open(target, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1024 * 1024)
            (staging / "firefox_data").mkdir(exist_ok=True)
            if load_saved_fingerprint(staging) is None:
                (staging / FINGERPRINT_FILENAME).unlink(missing_ok=True)  # regenerated on first use
            os.replace(staging, dest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    proxy_id = ""
    sp = manifest.get("saved_proxy")
    if isinstance(sp, dict) and proxies is not None:
        existing = next(
            (p for p in proxies.list()
             if (p.type, p.host, p.port, p.username) == (sp.get("type"), sp.get("host"), int(sp.get("port") or 0), sp.get("username") or "")),
            None,
        )
        proxy_id = existing.id if existing else proxies.create(sp).id
    existing_names = {p.name for p in store.list_profiles()}
    base_name = (name or str(raw.get("name") or "Imported profile")).strip()
    final_name = base_name if base_name not in existing_names else f"{base_name} (imported)"
    raw.update({"id": new_id, "name": final_name, "proxy_id": proxy_id, "deleted_at": None, "last_used_at": None})
    try:
        profile = Profile.from_dict(raw)
        store.insert_profile(profile)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return profile

