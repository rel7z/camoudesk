"""Change listeners and the background monitor that notices exits and adopts SDK runs.

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


class MonitorMixin(ManagerState):

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback fired (from a background thread) whenever any profile's status changes."""
        self._listeners.append(callback)


    def _notify(self) -> None:
        self._dirty = False
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                pass


    def start_monitor(self, interval: float = 0.5) -> None:
        """Poll for browsers that exit on their own (user quit, crash) and notify listeners."""
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor_stop.clear()

        def loop() -> None:
            ticks = 0
            while not self._monitor_stop.wait(interval):
                ticks += 1
                if ticks % 6 == 0:
                    self._adopt_running(only_new=True)  # e.g. an SDK script started meanwhile
                with self._lock:
                    self._reap_finished_locked()
                if self._dirty:
                    self._notify()

        self._monitor = threading.Thread(target=loop, name="camoudesk-process-monitor", daemon=True)
        self._monitor.start()


    def stop_monitor(self) -> None:
        self._monitor_stop.set()

