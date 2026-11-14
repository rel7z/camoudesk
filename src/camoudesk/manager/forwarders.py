"""Loopback proxy forwarders for direct-mode browsers, and proxy diagnostics in the profile log.

Part of ProcessManager (manager/process_manager.py); moved verbatim from it."""

# ruff: noqa: F401  (shared import block)
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, Dict, Iterable, List, Optional, Tuple

from camoudesk.core.launcher import Network, build_launch_spec, ensure_location, minimal_env, override_urls, resolve_network
from camoudesk.core.proxy_forwarder import ProxyForwarder
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.storage import ProfileStore, RegistryCorruptError
from camoudesk.manager.constants import _MODES, MODE_DIRECT, MODE_SDK, MODE_WORKER, _build_worker_env
from camoudesk.manager.runs import _group_alive, _pid_alive, _Run, process_start_time
from camoudesk.manager.state import ManagerState
from camoudesk.models.profile import Profile, ProxyConfig, sanitize_profile_id


class ForwarderMixin(ManagerState):

    def _proxy_logger(self, profile_id: str) -> Callable[[str], None]:
        """Append forwarder diagnostics (never credentials) to the profile's log."""
        log_path = self.store.log_file(profile_id)

        def write(message: str) -> None:
            self._warnings[profile_id] = message
            self._dirty = True
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"[PROXY] {message}\n")
            except OSError:
                pass

        return write


    def _failure_summary(self, profile_id: str, code: Optional[int]) -> str:
        """Human-readable reason for an unexpected exit, from CamouDesk's own log lines only.

        Firefox writes a lot of harmless noise to stderr (``Error in cpuinfo``, JS warnings), so
        the raw log tail is useless as an error message.
        """
        lines = self.read_logs(profile_id, max_lines=400).splitlines()
        tagged = [ln for ln in lines if ln.startswith(("[ERROR]", "[PROXY]"))]
        if not tagged:
            # A worker traceback ends with "SomeError: message".
            tagged = [ln for ln in lines if re.match(r"^[A-Za-z_][\w.]*(Error|Exception): ", ln)]
        status = f"Browser exited with status {code}" if code is not None else "Browser exited unexpectedly"
        return (tagged[-1].strip() if tagged else status)[:300]


    def _start_forwarder(self, profile: Profile, upstream: ProxyConfig, port: int = 0) -> ProxyForwarder:
        forwarder = ProxyForwarder(upstream, port=port, on_error=self._proxy_logger(profile.id))
        forwarder.start()
        return forwarder

