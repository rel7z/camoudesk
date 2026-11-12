"""Atomic JSON profile registry, persistent directory layout, and legacy migration."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Union

from camoudesk.config import (
    DEFAULT_STARTUP_URL,
    get_app_support_dir,
    get_legacy_profiles_dir,
    get_logs_dir,
    get_profiles_dir,
    get_registry_path,
)
from camoudesk.core.fingerprint import (
    ensure_profile_fingerprint,
    generate_and_save_fingerprint,
    summary_dict,
)
from camoudesk.models.profile import (
    Profile,
    ProxyConfig,
    clean_fp_overrides,
    clean_start_urls,
    clean_tags,
    parse_screen,
    normalize_country,
    normalize_ip,
    safe_ref,
    sanitize_profile_id,
    validate_os_target,
)


class RegistryCorruptError(RuntimeError):
    """Raised when registry.json exists but cannot be parsed; writes are refused to avoid data loss."""


# A registry entry is either a validated Profile or the raw dict of an entry that failed
# validation. Raw entries are never listed or launched, but they are written back untouched,
# so one hand-edited or newer-schema entry is never silently erased by an unrelated write.
_Entry = Union[Profile, Dict[str, Any]]


class ProfileStore:
    """Process- and thread-safe profile registry backed by atomic JSON writes and isolated folders."""

    def __init__(
        self,
        app_dir: Optional[Path] = None,
        legacy_dir: Optional[Path] = None,
    ) -> None:
        self.app_dir = (app_dir or get_app_support_dir()).resolve()
        self.profiles_dir = (self.app_dir / "profiles") if app_dir else get_profiles_dir()
        self.logs_dir = (self.app_dir / "logs") if app_dir else get_logs_dir()
        self.registry_path = (self.app_dir / "registry.json") if app_dir else get_registry_path()
        self.lock_path = self.registry_path.with_name("registry.lock")
        self.legacy_dir = (legacy_dir or get_legacy_profiles_dir()).resolve()
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the in-process lock plus an advisory flock shared with browser worker processes."""
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _initialize(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if self.registry_path.exists():
                return
            migrated = self._migrate_legacy_profiles()
            if migrated:
                self._write_entries(list(migrated))
            else:
                starter = self._build_new_profile(
                    profile_id="prof_default_1",
                    name="Profile 1",
                    os_target="macos",
                    timezone="auto",
                    locale="auto",
                    startup_url=DEFAULT_STARTUP_URL,
                )
                self._write_entries([starter])

    def _migrate_legacy_profiles(self) -> List[Profile]:
        """Import profiles from prototype ~/.camoufox_profiles if present."""
        legacy_registry = self.legacy_dir / "profiles_registry.json"
        if not legacy_registry.is_file():
            return []
        try:
            with open(legacy_registry, "r", encoding="utf-8") as stream:
                raw_list = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(raw_list, list):
            return []

        migrated: List[Profile] = []
        for item in raw_list:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                profile = Profile.from_dict(item)
            except ValueError:
                continue
            pdir = self.profile_dir(profile.id)
            pdir.mkdir(parents=True, exist_ok=True)
            self.firefox_data_dir(profile.id).mkdir(parents=True, exist_ok=True)
            _, summary = ensure_profile_fingerprint(pdir, profile.os)
            profile.fingerprint_summary = summary_dict(summary)
            migrated.append(profile)
        return migrated

    def _backup_corrupt_registry(self) -> Path:
        backup = self.registry_path.with_name(f"registry.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
        if not backup.exists():
            try:
                shutil.copy2(self.registry_path, backup)
            except OSError:
                pass
        return backup

    def _read_entries(self) -> List[_Entry]:
        """Read every registry entry, keeping invalid ones as raw dicts. Caller must hold the lock."""
        if not self.registry_path.is_file():
            return []
        try:
            with open(self.registry_path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            backup = self._backup_corrupt_registry()
            raise RegistryCorruptError(
                f"Profile registry {self.registry_path} is unreadable ({exc}). "
                f"CamouDesk will not overwrite it; a copy was saved to {backup}."
            ) from exc
        if not isinstance(raw, list):
            backup = self._backup_corrupt_registry()
            raise RegistryCorruptError(
                f"Profile registry {self.registry_path} is not a JSON list. "
                f"CamouDesk will not overwrite it; a copy was saved to {backup}."
            )

        entries: List[_Entry] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                entries.append(Profile.from_dict(entry))
            except (ValueError, KeyError, TypeError):
                entries.append(entry)
        return entries

    def _read_profiles(self) -> List[Profile]:
        return [e for e in self._read_entries() if isinstance(e, Profile)]

    def _write_entries(self, entries: List[_Entry]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [e.to_dict() if isinstance(e, Profile) else e for e in entries]
        fd, tmp_name = tempfile.mkstemp(prefix=".registry-", dir=str(self.registry_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
            os.replace(tmp_name, self.registry_path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _apply_inline_check(profile: Profile, check: Dict[str, Any]) -> None:
        """Cache the inline proxy's exit IP/timezone/country for "auto" timezone, locale and geoip."""
        profile.proxy_exit_ip = normalize_ip(check.get("ip"))
        profile.proxy_timezone = str(check.get("timezone") or "").strip()[:64]
        profile.proxy_country = normalize_country(check.get("country_code"))

    def record_inline_check(self, profile_id: str, check: Dict[str, Any]) -> None:
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            entries = self._read_entries()
            target = self._find(entries, safe_id)
            if target is not None and target.proxy:
                self._apply_inline_check(target, check)
                self._write_entries(entries)

    @staticmethod
    def _find(entries: List[_Entry], safe_id: str) -> Optional[Profile]:
        return next((e for e in entries if isinstance(e, Profile) and e.id == safe_id), None)

    def profile_dir(self, profile_id: str) -> Path:
        safe_id = sanitize_profile_id(profile_id)
        target = (self.profiles_dir / safe_id).resolve()
        if target.parent != self.profiles_dir.resolve():
            raise ValueError("Resolved profile directory escapes profiles root")
        return target

    def firefox_data_dir(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "firefox_data"

    def run_file(self, profile_id: str) -> Path:
        """Path of the running-process record used to re-adopt browsers after a GUI restart."""
        return self.profile_dir(profile_id) / "run.json"

    def log_file(self, profile_id: str) -> Path:
        safe_id = sanitize_profile_id(profile_id)
        return (self.logs_dir / f"{safe_id}.log").resolve()

    def _build_new_profile(
        self,
        profile_id: str,
        name: str,
        os_target: str = "macos",
        proxy: str = "",
        timezone: str = "auto",
        locale: str = "auto",
        startup_url: str = DEFAULT_STARTUP_URL,
        block_webrtc: bool = True,
        humanize: bool = True,
        notes: str = "",
        tags: Optional[List[str]] = None,
        proxy_exit_ip: str = "",
        start_urls: Optional[List[str]] = None,
        folder: str = "",
        proxy_id: str = "",
        fp_overrides: Optional[Dict[str, Any]] = None,
    ) -> Profile:
        safe_id = sanitize_profile_id(profile_id)
        norm_os = validate_os_target(os_target)
        norm_proxy = ProxyConfig.from_url(proxy).raw_url if proxy.strip() else ""

        pdir = self.profile_dir(safe_id)
        pdir.mkdir(parents=True, exist_ok=True)
        self.firefox_data_dir(safe_id).mkdir(parents=True, exist_ok=True)
        overrides = clean_fp_overrides({k: v for k, v in (fp_overrides or {}).items() if v is not None})
        if "webrtc" in overrides:
            block_webrtc = overrides["webrtc"] == "block"
        if overrides.get("screen"):
            _, summary = generate_and_save_fingerprint(pdir, norm_os, parse_screen(overrides["screen"]))
        else:
            _, summary = ensure_profile_fingerprint(pdir, norm_os)
        urls = clean_start_urls(start_urls if start_urls else [startup_url or DEFAULT_STARTUP_URL])

        return Profile(
            id=safe_id,
            name=name.strip() or safe_id,
            os=norm_os,
            proxy=norm_proxy,
            proxy_exit_ip=normalize_ip(proxy_exit_ip) if norm_proxy else "",
            timezone=(timezone or "auto").strip(),
            locale=(locale or "auto").strip(),
            startup_url=urls[0] if urls else "",
            start_urls=urls,
            block_webrtc=bool(block_webrtc),
            humanize=bool(humanize),
            notes=(notes or "").strip(),
            folder=(folder or "").strip()[:80],
            tags=clean_tags(tags or []),
            proxy_id=safe_ref(proxy_id),
            fp_overrides=overrides,
            fingerprint_summary=summary_dict(summary),
        )

    def list_profiles(self) -> List[Profile]:
        with self._locked():
            return self._read_profiles()

    def get_profile(self, profile_id: str) -> Optional[Profile]:
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            return self._find(self._read_entries(), safe_id)

    def create_profile(self, data: Dict[str, Any]) -> Profile:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Profile name is required")
        profile_id = f"prof_{int(time.time())}_{os.urandom(2).hex()}"
        profile = self._build_new_profile(
            profile_id=profile_id,
            name=name,
            os_target=str(data.get("os") or "macos"),
            proxy=str(data.get("proxy") or ""),
            timezone=str(data.get("timezone") or "auto"),
            locale=str(data.get("locale") or "auto"),
            startup_url=str(data.get("startup_url") or data.get("target_url") or DEFAULT_STARTUP_URL),
            block_webrtc=bool(data.get("block_webrtc", True)),
            humanize=bool(data.get("humanize", True)),
            notes=str(data.get("notes") or ""),
            tags=data.get("tags") if isinstance(data.get("tags"), list) else [],
            proxy_exit_ip=str(data.get("proxy_exit_ip") or ""),
            start_urls=data.get("start_urls") if isinstance(data.get("start_urls"), list) else None,
            folder=str(data.get("folder") or ""),
            proxy_id=str(data.get("proxy_id") or ""),
            fp_overrides=data.get("fp_overrides") if isinstance(data.get("fp_overrides"), dict) else None,
        )
        if isinstance(data.get("proxy_check"), dict) and profile.proxy:
            self._apply_inline_check(profile, data["proxy_check"])
        with self._locked():
            entries = self._read_entries()
            entries.insert(0, profile)
            self._write_entries(entries)
        return profile

    def update_profile(self, profile_id: str, updates: Dict[str, Any]) -> Profile:
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            entries = self._read_entries()
            target = self._find(entries, safe_id)
            if target is None:
                raise KeyError(f"Profile '{safe_id}' not found")

            if "name" in updates:
                new_name = str(updates["name"] or "").strip()
                if not new_name:
                    raise ValueError("Profile name cannot be empty")
                target.name = new_name
            if "proxy" in updates:
                raw_proxy = str(updates["proxy"] or "").strip()
                new_proxy = ProxyConfig.from_url(raw_proxy).raw_url if raw_proxy else ""
                if new_proxy != target.proxy:
                    # Cached exit data belongs to the old proxy; never geo-match against it.
                    target.proxy_exit_ip = target.proxy_timezone = target.proxy_country = ""
                target.proxy = new_proxy
            if "proxy_exit_ip" in updates:
                target.proxy_exit_ip = normalize_ip(updates["proxy_exit_ip"]) if target.proxy else ""
            if isinstance(updates.get("proxy_check"), dict) and target.proxy:
                self._apply_inline_check(target, updates["proxy_check"])
            if "proxy_id" in updates:
                target.proxy_id = safe_ref(updates["proxy_id"])
            if "start_urls" in updates:
                target.set_start_urls(updates["start_urls"])
            if "folder" in updates:
                target.folder = str(updates["folder"] or "").strip()[:80]
            if "tags" in updates:
                target.tags = clean_tags(updates["tags"])
            if "fp_overrides" in updates and isinstance(updates["fp_overrides"], dict):
                merged = {**target.fp_overrides, **updates["fp_overrides"]}
                target.fp_overrides = clean_fp_overrides({k: v for k, v in merged.items() if v is not None})
                if "webrtc" in target.fp_overrides:
                    target.block_webrtc = target.fp_overrides["webrtc"] == "block"
            elif "block_webrtc" in updates:
                target.fp_overrides.pop("webrtc", None)
            if "timezone" in updates:
                target.timezone = str(updates["timezone"] or "auto").strip()
            if "locale" in updates:
                target.locale = str(updates["locale"] or "auto").strip()
            if ("startup_url" in updates or "target_url" in updates) and "start_urls" not in updates:
                url_val = updates.get("startup_url") or updates.get("target_url") or DEFAULT_STARTUP_URL
                target.set_start_urls([url_val] + target.start_urls[1:])
            if "block_webrtc" in updates:
                target.block_webrtc = bool(updates["block_webrtc"])
            if "humanize" in updates:
                target.humanize = bool(updates["humanize"])
            if "notes" in updates:
                target.notes = str(updates["notes"] or "").strip()
            if "os" in updates:
                new_os = validate_os_target(str(updates["os"]))
                if new_os != target.os:
                    target.os = new_os
                    # A GPU recorded for the old OS is not valid for the new one.
                    target.fp_overrides.pop("webgl", None)
                    try:
                        _, summary = generate_and_save_fingerprint(
                            self.profile_dir(safe_id), new_os, parse_screen(target.fp_overrides.get("screen"))
                        )
                    except ValueError:
                        target.fp_overrides.pop("screen", None)
                        _, summary = generate_and_save_fingerprint(self.profile_dir(safe_id), new_os)
                    target.fingerprint_summary = summary_dict(summary)

            self._write_entries(entries)
            return target

    _KEEP = object()

    def regenerate_fingerprint(self, profile_id: str, screen: Any = _KEEP) -> Profile:
        """New fingerprint for the profile's OS. ``screen``: "WxH" to pin, "" for any size,
        omitted to keep the previously pinned size."""
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            entries = self._read_entries()
            target = self._find(entries, safe_id)
            if target is None:
                raise KeyError(f"Profile '{safe_id}' not found")
            if screen is not ProfileStore._KEEP:
                if screen and not parse_screen(screen):
                    raise ValueError("Screen must look like 1920x1080")
                if screen:
                    target.fp_overrides["screen"] = f"{parse_screen(screen)[0]}x{parse_screen(screen)[1]}"
                else:
                    target.fp_overrides.pop("screen", None)
            pdir = self.profile_dir(safe_id)
            pdir.mkdir(parents=True, exist_ok=True)
            _, summary = generate_and_save_fingerprint(pdir, target.os, parse_screen(target.fp_overrides.get("screen")))
            target.fingerprint_summary = summary_dict(summary)
            self._write_entries(entries)
            return target

    def mark_used(self, profile_id: str) -> None:
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            entries = self._read_entries()
            target = self._find(entries, safe_id)
            if target is not None:
                target.last_used_at = time.strftime("%Y-%m-%d %H:%M")
                self._write_entries(entries)

    def delete_profile(self, profile_id: str, wipe_data: bool = True) -> bool:
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            entries = self._read_entries()
            remaining = [e for e in entries if not (isinstance(e, Profile) and e.id == safe_id)]
            if len(remaining) == len(entries):
                return False
            self._write_entries(remaining)

        if wipe_data:
            pdir = self.profile_dir(safe_id)
            if pdir.exists():
                shutil.rmtree(pdir, ignore_errors=True)
            lfile = self.log_file(safe_id)
            if lfile.exists():
                lfile.unlink(missing_ok=True)
        return True

    # ------------------------------------------------------------------ trash, bulk, clone

    def set_trashed(self, profile_id: str, trashed: bool) -> Profile:
        """Move a profile to the trash (data kept) or restore it."""
        safe_id = sanitize_profile_id(profile_id)
        with self._locked():
            entries = self._read_entries()
            target = self._find(entries, safe_id)
            if target is None:
                raise KeyError(f"Profile '{safe_id}' not found")
            target.deleted_at = time.strftime("%Y-%m-%d %H:%M") if trashed else None
            self._write_entries(entries)
            return target

    def bulk_update_metadata(
        self,
        profile_ids: List[str],
        folder: Optional[str] = None,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
    ) -> int:
        """Folder/tag changes for many profiles in one write. These never affect a running browser."""
        ids = {sanitize_profile_id(i) for i in profile_ids}
        add = clean_tags(add_tags or [])
        remove = set(clean_tags(remove_tags or []))
        changed = 0
        with self._locked():
            entries = self._read_entries()
            for e in entries:
                if isinstance(e, Profile) and e.id in ids:
                    if folder is not None:
                        e.folder = str(folder).strip()[:80]
                    e.tags = clean_tags([t for t in e.tags if t not in remove] + add)
                    changed += 1
            self._write_entries(entries)
        return changed

    def rename_folder(self, old: str, new: str) -> int:
        old_name, new_name = str(old or "").strip(), str(new or "").strip()[:80]
        if not old_name:
            raise ValueError("Choose a folder to rename")
        changed = 0
        with self._locked():
            entries = self._read_entries()
            for e in entries:
                if isinstance(e, Profile) and e.folder == old_name:
                    e.folder = new_name
                    changed += 1
            self._write_entries(entries)
        return changed

    _COPY_SKIP = {"cache2", "startupCache", "crashes", "minidumps", "saved-telemetry-pings", "datareporting",
                  "lock", ".parentlock", "parent.lock", "storage.sqlite-journal"}

    def clone_profile(self, profile_id: str, name: str = "", copy_data: bool = False) -> Profile:
        """Clone a profile's settings. ``copy_data`` also copies its fingerprint, cookies and site
        data (a duplicate the sites will see as the same device); otherwise a fresh fingerprint
        is generated for the same OS and pinned screen size."""
        src = self.get_profile(profile_id)
        if src is None:
            raise KeyError(f"Profile '{profile_id}' not found")
        data = src.to_dict()
        for key in ("id", "created_at", "last_used_at", "deleted_at", "fingerprint_summary", "startup_url"):
            data.pop(key, None)
        data["name"] = (name or "").strip() or f"{src.name} (copy)"
        new_id = f"prof_{int(time.time())}_{os.urandom(2).hex()}"
        if copy_data:
            src_dir, dst_dir = self.profile_dir(src.id), self.profile_dir(new_id)
            if dst_dir.exists():
                raise RuntimeError("Target profile folder already exists")
            shutil.copytree(src_dir, dst_dir, ignore=lambda _d, names: [n for n in names if n in self._COPY_SKIP or n == "run.json"])
        profile = self._build_new_profile(
            profile_id=new_id,
            name=data["name"],
            os_target=data["os"],
            proxy=data.get("proxy") or "",
            timezone=data.get("timezone") or "auto",
            locale=data.get("locale") or "auto",
            block_webrtc=data.get("block_webrtc", True),
            humanize=data.get("humanize", True),
            notes=data.get("notes") or "",
            tags=data.get("tags") or [],
            proxy_exit_ip=data.get("proxy_exit_ip") or "",
            start_urls=data.get("start_urls") or None,
            folder=data.get("folder") or "",
            proxy_id=data.get("proxy_id") or "",
            # A duplicate keeps the copied fingerprint.json: leave out the screen pin here, since a
            # pin makes the builder generate (and overwrite) a fingerprint. Clones get a fresh one.
            fp_overrides={k: v for k, v in (data.get("fp_overrides") or {}).items() if not (copy_data and k == "screen")},
        )
        profile.fp_overrides = clean_fp_overrides(data.get("fp_overrides") or {})
        profile.proxy_timezone, profile.proxy_country = src.proxy_timezone, src.proxy_country
        with self._locked():
            entries = self._read_entries()
            entries.insert(0, profile)
            self._write_entries(entries)
        return profile

    def insert_profile(self, profile: Profile) -> Profile:
        """Register an already-built profile (e.g. an imported archive) at the top of the list."""
        with self._locked():
            entries = self._read_entries()
            if self._find(entries, profile.id) is not None:
                raise ValueError(f"Profile '{profile.id}' already exists")
            entries.insert(0, profile)
            self._write_entries(entries)
        return profile
