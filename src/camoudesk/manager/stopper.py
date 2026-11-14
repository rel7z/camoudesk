"""Graceful stop: Apple Event quit first, then SIGTERM/SIGKILL of the process group (invariant 5).

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


def _request_app_quit(pid: int) -> bool:
    """Ask a macOS app to quit like Cmd+Q (Apple Event), so Firefox flushes cookies and session."""
    try:
        import AppKit  # provided by pyobjc, a pywebview dependency on macOS
    except Exception:
        return False
    try:
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return bool(app is not None and app.terminate())
    except Exception:
        return False


class StopperMixin(ManagerState):

    @staticmethod
    def _signal(run: _Run, sig: int, group: bool) -> None:
        try:
            if group:
                os.killpg(run.pgid, sig)
            else:
                os.kill(run.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


    @staticmethod
    def _gone(run: _Run) -> bool:
        if run.mode == MODE_SDK:
            return not _pid_alive(run.pid)  # a script is not necessarily a process-group leader
        if run.proc is not None:
            run.proc.poll()  # reap the leader so it does not linger as a zombie group member
        return not _group_alive(run.pgid)


    def _wait_gone(self, runs: Iterable[_Run], timeout: float) -> List[_Run]:
        pending = list(runs)
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            pending = [r for r in pending if not self._gone(r)]
            if pending:
                time.sleep(0.1)
        return [r for r in pending if not self._gone(r)]


    def _terminate(self, runs: List[_Run], timeout: float) -> List[_Run]:
        """Graceful quit, then SIGTERM, then SIGKILL of each whole process group. Returns survivors."""
        # Never signal an adopted PID that has since been recycled by an unrelated process.
        runs = [r for r in runs if r.proc is not None or r.alive(strict=True)]
        for run in runs:
            if run.mode == MODE_DIRECT:
                if not _request_app_quit(run.pid):
                    self._signal(run, signal.SIGTERM, group=False)
            else:
                # The worker closes its Playwright context on SIGTERM.
                self._signal(run, signal.SIGTERM, group=False)
        pending = self._wait_gone(runs, timeout)

        # SDK scripts are signalled by PID only: their process group may be the user's shell job.
        for run in pending:
            self._signal(run, signal.SIGTERM, group=run.mode != MODE_SDK)
        pending = self._wait_gone(pending, 2.0)

        for run in pending:
            self._signal(run, signal.SIGKILL, group=run.mode != MODE_SDK)
        return self._wait_gone(pending, 2.0)


    def stop(self, profile_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            run = self._runs.pop(safe_id, None)
        if run is None:
            return {"ok": True, "was_running": False, "exited": True}

        survivors = self._terminate([run], timeout)
        with self._lock:
            if survivors:
                self._runs[safe_id] = run
                return {"ok": False, "was_running": True, "exited": False, "error": "Browser did not exit"}
            self._finish_locked(safe_id, run)
            self._last_errors.pop(safe_id, None)
        return {"ok": True, "was_running": True, "exited": True}


    def stop_async(self, profile_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Begin a graceful stop in the background; listeners are notified when it completes."""
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            if safe_id not in self._runs:
                return {"ok": True, "was_running": False, "stopping": False}
            if self._transitions.get(safe_id) == "stopping":
                return {"ok": True, "was_running": True, "stopping": True}
            self._transitions[safe_id] = "stopping"
        self._notify()

        def run() -> None:
            try:
                result = self.stop(safe_id, timeout=timeout)
                if not result.get("exited", True):
                    with self._lock:
                        self._last_errors[safe_id] = "The browser did not exit after stop."
            finally:
                with self._lock:
                    self._transitions.pop(safe_id, None)
                self._notify()

        threading.Thread(target=run, name=f"camoudesk-stop-{safe_id}", daemon=True).start()
        return {"ok": True, "was_running": True, "stopping": True}


    def stop_all(self, timeout: Optional[float] = None, include_sdk: bool = False) -> None:
        """Stop every browser in parallel so quitting the GUI waits at most ~timeout, not N*timeout.

        SDK runs are the user's own scripts, not browsers this app started: they are left alone
        unless ``include_sdk`` (a manual Stop on such a profile still works).
        """
        with self._lock:
            self._reap_finished_locked()
            ids = [pid for pid, run in self._runs.items() if include_sdk or run.mode != MODE_SDK]
        self.stop_many(ids, timeout=timeout)


    def stop_many(self, profile_ids: List[str], timeout: Optional[float] = None) -> Dict[str, bool]:
        """Stop several profiles in parallel (one shared timeout). Returns {profile_id: exited}."""
        timeout = self.stop_timeout if timeout is None else timeout
        safe_ids = [sanitize_profile_id(i) for i in profile_ids]
        with self._lock:
            self._reap_finished_locked()
            items = [(i, self._runs.pop(i)) for i in safe_ids if i in self._runs]
            for i, _ in items:
                self._transitions[i] = "stopping"
        if items:
            self._notify()
        survivors = set(id(r) for r in self._terminate([r for _, r in items], timeout))
        result = {i: True for i in safe_ids}
        with self._lock:
            for profile_id, run in items:
                self._transitions.pop(profile_id, None)
                if id(run) in survivors:
                    self._runs[profile_id] = run
                    result[profile_id] = False
                else:
                    self._finish_locked(profile_id, run)
                    self._last_errors.pop(profile_id, None)
        if items:
            self._notify()
        return result

