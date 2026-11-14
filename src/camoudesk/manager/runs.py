"""run.json records, re-adoption of running browsers, reaping and status queries.

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
from camoudesk.manager.state import ManagerState
from camoudesk.models.profile import Profile, ProxyConfig, sanitize_profile_id


def process_start_time(pid: int) -> str:
    """Kernel start time of pid (``ps -o lstart``); guards re-adoption against PID reuse."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class _Run:
    pid: int
    pgid: int
    mode: str
    started: str
    proc: Optional["subprocess.Popen[Any]"] = None
    log_fh: Optional[IO[str]] = None
    forward_port: int = 0
    forwarder: Optional[ProxyForwarder] = None

    def alive(self, strict: bool = False) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if not _pid_alive(self.pid):
            return False
        # Adopted (not our child): the start time is checked at adoption and before any signal,
        # so a recycled PID can never be signalled; status polls use the cheap kill(0) check.
        return not strict or process_start_time(self.pid) == self.started

    def exit_code(self) -> Optional[int]:
        return self.proc.poll() if self.proc is not None else None

    def to_record(self) -> Dict[str, Any]:
        rec: Dict[str, Any] = {"pid": self.pid, "pgid": self.pgid, "mode": self.mode, "started": self.started}
        if self.forward_port:
            rec["forward_port"] = self.forward_port
        return rec


class RunRegistryMixin(ManagerState):

    def _write_run_record(self, profile_id: str, run: _Run) -> None:
        path = self.store.run_file(profile_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(run.to_record()), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


    def _clear_run_record(self, profile_id: str) -> None:
        try:
            self.store.run_file(profile_id).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


    def _adopt_running(self, only_new: bool = False) -> None:
        """Re-attach to browsers recorded in run.json that are still the same live process.

        ``only_new`` (periodic rescans) skips profiles this manager already tracks or is
        starting/stopping, so it never touches records of its own launches.
        """
        try:
            profiles = self.store.list_profiles()
        except RegistryCorruptError:
            return
        for profile in profiles:
            if only_new:
                with self._lock:
                    if profile.id in self._runs or profile.id in self._transitions:
                        continue
            path = self.store.run_file(profile.id)
            if not path.is_file():
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                run = _Run(
                    pid=int(rec["pid"]),
                    pgid=int(rec.get("pgid") or rec["pid"]),
                    mode=str(rec.get("mode") or MODE_WORKER),
                    started=str(rec.get("started") or ""),
                    forward_port=int(rec.get("forward_port") or 0),
                )
            except (OSError, ValueError, KeyError, TypeError):
                self._clear_run_record(profile.id)
                continue
            if not (run.started and run.alive(strict=True)):
                with self._lock:
                    if profile.id not in self._runs:
                        self._clear_run_record(profile.id)
                continue
            with self._lock:
                if profile.id in self._runs or profile.id in self._transitions:
                    continue
                self._runs[profile.id] = run
                self._dirty = True
            upstream = resolve_network(profile, self.proxies).proxy
            if run.forward_port and upstream.has_credentials:
                # The forwarder died with the previous GUI process; the browser's prefs still
                # point at this port, so bring it back there to restore its network access.
                try:
                    run.forwarder = self._start_forwarder(profile, upstream, port=run.forward_port)
                except Exception as exc:
                    self._proxy_logger(profile.id)(
                        f"Could not restart the proxy forwarder on port {run.forward_port} ({exc}); "
                        "restart this profile to restore its network access."
                    )


    def _finish_locked(self, profile_id: str, run: _Run) -> None:
        if run.forwarder is not None:
            run.forwarder.stop()
            run.forwarder = None
        if run.log_fh is not None:
            try:
                run.log_fh.close()
            except OSError:
                pass
        self._clear_run_record(profile_id)


    def _reap_finished_locked(self) -> None:
        """Clean up exited browsers and record non-zero exit diagnostics."""
        for profile_id, run in list(self._runs.items()):
            if run.alive():
                continue
            self._runs.pop(profile_id, None)
            self._finish_locked(profile_id, run)
            self._warnings.pop(profile_id, None)
            self._dirty = True
            code = run.exit_code()
            if code not in (None, 0):
                self._last_errors[profile_id] = self._failure_summary(profile_id, code)


    def is_running(self, profile_id: str) -> bool:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            return safe_id in self._runs


    def get_status(self, profile_id: str) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            run = self._runs.get(safe_id)
            transition = self._transitions.get(safe_id)
            if run is not None:
                return {
                    "status": transition or "running",
                    "pid": run.pid,
                    "mode": run.mode,
                    "last_error": "",
                    "warning": self._warnings.get(safe_id, ""),
                }
            return {
                "status": transition or "stopped",
                "pid": None,
                "mode": None,
                "last_error": "" if transition else self._last_errors.get(safe_id, ""),
                "warning": "",
            }

