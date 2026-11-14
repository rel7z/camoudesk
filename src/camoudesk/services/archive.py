"""ArchiveService: export profiles to .camoudesk files and import them (core/profile_archive.py)."""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

from camoudesk.core import profile_archive
from camoudesk.models.profile import Profile
from camoudesk.services.errors import Invalid, NotFound

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.services.context import AppContext


class ArchiveService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    @staticmethod
    def archive_name(profile: Profile) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in profile.name).strip()[:50] or profile.id
        return f"{safe}{profile_archive.SUFFIX}"

    def default_filename(self, profile_id: str) -> str:
        profile = self.ctx.store.get_profile(profile_id)
        if profile is None:
            raise NotFound("Profile not found")
        return self.archive_name(profile)

    def export_one(self, profile_id: str, dest: Path, include_credentials: bool = False) -> Dict[str, Any]:
        if self.ctx.manager.is_running(profile_id):
            raise Invalid("Stop the profile before exporting it.")
        return profile_archive.export_profile(self.ctx.store, self.ctx.proxies, profile_id, Path(dest), bool(include_credentials))

    def export_many(self, profile_ids: List[str], folder: str, include_credentials: bool = False) -> Dict[str, Any]:
        saved, errors = 0, []
        for pid in profile_ids or []:
            profile = self.ctx.store.get_profile(pid)
            if profile is None:
                continue
            dest = Path(folder) / self.archive_name(profile)
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}-{profile.id[-4:]}{profile_archive.SUFFIX}")
            try:
                self.export_one(pid, dest, include_credentials)
                saved += 1
            except (ValueError, OSError) as exc:
                errors.append(f"{profile.name}: {exc}")
        return {"saved": saved, "folder": folder, "errors": errors}

    def import_paths(self, paths: List[str]) -> Dict[str, Any]:
        imported, errors, needs_password = [], [], 0
        for path in paths:
            try:
                manifest = profile_archive.read_manifest(Path(path))
                prof = profile_archive.import_profile(self.ctx.store, self.ctx.proxies, Path(path))
                imported.append(prof.name)
                has_proxy = manifest["profile"].get("proxy") or manifest.get("saved_proxy")
                if has_proxy and not manifest.get("includes_credentials"):
                    needs_password += 1
            except (ValueError, KeyError, OSError) as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
        return {"imported": len(imported), "names": imported, "errors": errors, "needs_proxy_password": needs_password}
