"""EngineAdapter: everything that depends on the browser engine, behind one interface.

Services, the process manager, the worker and the SDK reach the engine only through an adapter, so
another engine (e.g. a Chromium build, should Firefox-based anti-detect engines stall) can be added
without touching them. Rules every adapter must keep (CLAUDE.md):

* spoofing happens inside the engine, never through JavaScript injected into pages;
* the same profile gets identical identity values from every launch path (direct, worker, SDK);
* a saved identity is reused as-is; it is only replaced on an explicit regenerate/OS change;
* the host is probed outside launches (no nested browser launches inside a launch).
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from camoudesk.models.profile import FingerprintSummary, Profile


class EngineAdapter(ABC):
    name: str = ""

    # ------------------------------------------------------------- identities

    @abstractmethod
    def generate_identity(self, os_target: str, screen: Optional[Tuple[int, int]] = None, source: str = "generated",
                          gpu_renderer: Optional[str] = None) -> Dict[str, Any]:
        """A new identity for ``os_target``: sampled ("generated") or a recorded real device ("preset")."""

    @abstractmethod
    def verify_identity(self, identity: Dict[str, Any], os_target: str) -> bool:
        """Whether every signal of ``identity`` names ``os_target`` (never trust a generator blindly)."""

    @abstractmethod
    def summarize(self, os_target: str, identity: Dict[str, Any]) -> FingerprintSummary:
        """What websites will see, for the UI."""

    @abstractmethod
    def load_identity(self, profile_dir: Path) -> Optional[Dict[str, Any]]:
        """The profile's saved identity, or None."""

    # ------------------------------------------------------------- launching

    @abstractmethod
    def prepare_host(self, app_dir: Path) -> None:
        """Probe the host once, outside any launch (e.g. its GPU for the canvas policy)."""

    @abstractmethod
    def canvas_policy(self, app_dir: Path, profile: Profile, identity: Dict[str, Any]) -> Optional[bool]:
        """True covers the canvas; otherwise the profile shows its real one (see the engine)."""

    @abstractmethod
    def context_kwargs(self, profile: Profile, store: Any, network: Any, headless: bool) -> Tuple[Dict[str, Any], Optional[str]]:
        """Arguments for an in-process persistent browser context (worker and SDK paths)."""

    @abstractmethod
    def launch_spec(self, profile: Profile, store: Any, network: Any, headless: bool = False,
                    url_override: Any = None, local_proxy: Any = None) -> Any:
        """Executable, arguments and environment for a direct launch (headed path)."""

    @abstractmethod
    def binary_status(self) -> Dict[str, Any]:
        """Whether the engine's browser is installed, and where."""

    # ------------------------------------------------------------- options for the UI

    @abstractmethod
    def gpu_options(self, os_target: str) -> List[List[str]]:
        """Graphics cards this engine can claim for ``os_target``."""

    @abstractmethod
    def screen_presets(self, os_target: str) -> List[str]:
        """Common screen sizes for ``os_target``."""
