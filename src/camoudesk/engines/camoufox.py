"""CamoufoxEngine: Firefox (Camoufox) with identities applied by C++ patches (CAMOU_CONFIG_*).

Delegates to the Camoufox-specific modules: core/fingerprint.py (fpgen samples and recorded
presets), core/launcher.py (one kwargs builder for every launch path, the direct launch spec,
canvas policy) and core/host_gpu.py (the host GPU probe that must never run inside a launch).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from camoudesk.engines.base import EngineAdapter
from camoudesk.models.profile import FingerprintSummary, Profile


class CamoufoxEngine(EngineAdapter):
    name = "camoufox"

    def generate_identity(self, os_target: str, screen: Optional[Tuple[int, int]] = None, source: str = "generated",
                          gpu_renderer: Optional[str] = None) -> Dict[str, Any]:
        from camoudesk.core.fingerprint import generate_preset_fingerprint, generate_verified_fingerprint

        if source == "preset":
            return generate_preset_fingerprint(os_target, screen, gpu_renderer=gpu_renderer)
        return generate_verified_fingerprint(os_target, screen)

    def verify_identity(self, identity: Dict[str, Any], os_target: str) -> bool:
        from camoudesk.core.fingerprint import fingerprint_matches_os

        return fingerprint_matches_os(identity, os_target)

    def summarize(self, os_target: str, identity: Dict[str, Any]) -> FingerprintSummary:
        from camoudesk.core.fingerprint import summarize

        return summarize(os_target, identity)

    def load_identity(self, profile_dir: Path) -> Optional[Dict[str, Any]]:
        from camoudesk.core.fingerprint import load_saved_fingerprint

        return load_saved_fingerprint(profile_dir)

    def prepare_host(self, app_dir: Path) -> None:
        from camoudesk.core import host_gpu

        host_gpu.ensure(app_dir)

    def host_gpu(self, app_dir: Path) -> Optional[Tuple[str, str]]:
        """The stored host GPU, without probing."""
        from camoudesk.core import host_gpu

        return host_gpu.cached(app_dir)[1]

    @property
    def host_os(self) -> str:
        from camoudesk.core import host_gpu

        return host_gpu.HOST_OS

    def canvas_policy(self, app_dir: Path, profile: Profile, identity: Dict[str, Any]) -> Optional[bool]:
        from camoudesk.core.launcher import canvas_policy

        return canvas_policy(app_dir, profile, identity)

    def context_kwargs(self, profile: Profile, store: Any, network: Any, headless: bool) -> Tuple[Dict[str, Any], Optional[str]]:
        from camoudesk.core.launcher import profile_camoufox_kwargs

        return profile_camoufox_kwargs(profile, store, network, headless=headless)

    def launch_spec(self, profile: Profile, store: Any, network: Any, headless: bool = False,
                    url_override: Any = None, local_proxy: Any = None) -> Any:
        from camoudesk.core.launcher import build_launch_spec

        return build_launch_spec(profile, store, headless=headless, url_override=url_override,
                                 local_proxy=local_proxy, network=network)

    def binary_status(self) -> Dict[str, Any]:
        from camoudesk.core import host_gpu

        path = host_gpu.browser_binary()
        version = ""
        if path:
            try:
                from camoufox.utils import resolve_verstr

                version = str(resolve_verstr(None))
            except Exception:
                version = ""
        return {"engine": self.name, "installed": bool(path and Path(path).exists()), "path": path or "", "version": version}

    def gpu_options(self, os_target: str) -> List[List[str]]:
        from camoudesk.core.launcher import gpu_options

        return gpu_options(os_target)

    def screen_presets(self, os_target: str) -> List[str]:
        from camoudesk.core.launcher import SCREEN_PRESETS

        return list(SCREEN_PRESETS[os_target])
