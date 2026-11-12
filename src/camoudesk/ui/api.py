"""Typed JavaScript-to-Python API bridge exposed through PyWebView."""

from typing import Any, Dict, List

from camoudesk import __version__
from camoudesk.core.proxy import check_proxy_health
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.models.profile import Profile, ProxyConfig


def _serialize_profile_for_ui(profile: Profile, status_info: Dict[str, Any]) -> Dict[str, Any]:
    data = profile.to_dict()
    data["status"] = status_info["status"]
    data["pid"] = status_info["pid"]
    data["last_error"] = status_info["last_error"]
    data["mode"] = status_info.get("mode")
    data["warning"] = status_info.get("warning", "")
    data["proxy_masked"] = ProxyConfig.from_url(profile.proxy).masked_display() if profile.proxy else "Direct"
    return data


class DesktopBridgeApi:
    """Bridge methods callable asynchronously from window.pywebview.api."""

    def __init__(self, store: ProfileStore, manager: ProcessManager) -> None:
        self.store = store
        self.manager = manager

    def get_app_info(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "data": {
                "version": __version__,
                "data_dir": str(self.store.app_dir),
            },
        }

    def list_profiles(self) -> Dict[str, Any]:
        try:
            profiles = self.store.list_profiles()
            items: List[Dict[str, Any]] = []
            for prof in profiles:
                st = self.manager.get_status(prof.id)
                items.append(_serialize_profile_for_ui(prof, st))
            return {"ok": True, "data": items}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def create_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            profile = self.store.create_profile(payload or {})
            st = self.manager.get_status(profile.id)
            return {"ok": True, "data": _serialize_profile_for_ui(profile, st)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update_profile(self, profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if self.manager.is_running(profile_id):
                return {"ok": False, "error": "Stop the running browser before editing this profile."}
            profile = self.store.update_profile(profile_id, payload or {})
            st = self.manager.get_status(profile.id)
            return {"ok": True, "data": _serialize_profile_for_ui(profile, st)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def regenerate_fingerprint(self, profile_id: str) -> Dict[str, Any]:
        try:
            if self.manager.is_running(profile_id):
                return {"ok": False, "error": "Stop the running browser before regenerating its hardware fingerprint."}
            profile = self.store.regenerate_fingerprint(profile_id)
            st = self.manager.get_status(profile.id)
            return {"ok": True, "data": _serialize_profile_for_ui(profile, st)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_profile(self, profile_id: str) -> Dict[str, Any]:
        try:
            stopped = self.manager.stop(profile_id)
            if not stopped.get("exited", True):
                return {"ok": False, "error": "The browser is still running; profile data was not deleted."}
            deleted = self.store.delete_profile(profile_id, wipe_data=True)
            if not deleted:
                return {"ok": False, "error": "Profile not found"}
            return {"ok": True, "data": {"id": profile_id}}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def launch_profile(self, profile_id: str) -> Dict[str, Any]:
        try:
            return self.manager.launch(profile_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_profile(self, profile_id: str) -> Dict[str, Any]:
        """Starts a graceful stop and returns at once; the UI is notified when it finishes."""
        try:
            return self.manager.stop_async(profile_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_diagnostics(self, profile_id: str) -> Dict[str, Any]:
        try:
            return {"ok": True, "data": self.manager.diagnostics(profile_id)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_profile_logs(self, profile_id: str) -> Dict[str, Any]:
        try:
            logs = self.manager.read_logs(profile_id, max_lines=120)
            return {"ok": True, "data": logs}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def test_proxy(self, proxy_url: str) -> Dict[str, Any]:
        return check_proxy_health(proxy_url)
