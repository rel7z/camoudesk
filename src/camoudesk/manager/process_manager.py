"""Subprocess lifecycle tracking, log capture, and graceful shutdown for browser workers."""

import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Dict, Optional

from camoudesk.core.storage import ProfileStore
from camoudesk.models.profile import sanitize_profile_id


def _build_worker_env() -> Dict[str, str]:
    """Ensure child worker inherits current environment and can import camoudesk."""
    env = dict(os.environ)
    src_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if src_root not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src_root, *parts])
    return env


class ProcessManager:
    """Tracks running browser worker subprocesses per profile ID."""

    def __init__(
        self,
        store: ProfileStore,
        python_executable: Optional[str] = None,
    ) -> None:
        self.store = store
        self.python_executable = python_executable or sys.executable
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen[Any]] = {}
        self._log_handles: Dict[str, Any] = {}
        self._last_errors: Dict[str, str] = {}

    def _reap_finished_locked(self) -> None:
        """Clean up exited child processes and record non-zero exit diagnostics."""
        finished = []
        for profile_id, proc in self._processes.items():
            code = proc.poll()
            if code is not None:
                finished.append((profile_id, code))

        for profile_id, code in finished:
            self._processes.pop(profile_id, None)
            handle = self._log_handles.pop(profile_id, None)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if code != 0:
                tail = self.read_logs(profile_id, max_lines=10)
                self._last_errors[profile_id] = tail or f"Worker exited with status {code}"

    def is_running(self, profile_id: str) -> bool:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            return safe_id in self._processes

    def get_status(self, profile_id: str) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            proc = self._processes.get(safe_id)
            if proc is not None:
                return {"status": "running", "pid": proc.pid, "last_error": ""}
            return {"status": "stopped", "pid": None, "last_error": self._last_errors.get(safe_id, "")}

    def launch(self, profile_id: str, headless: bool = False, url_override: Optional[str] = None) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        profile = self.store.get_profile(safe_id)
        if profile is None:
            return {"ok": False, "error": f"Profile '{safe_id}' does not exist"}

        with self._lock:
            self._reap_finished_locked()
            if safe_id in self._processes:
                return {"ok": True, "pid": self._processes[safe_id].pid, "already_running": True}

            self._last_errors.pop(safe_id, None)
            log_path = self.store.log_file(safe_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "w", encoding="utf-8")

            cmd = [
                self.python_executable,
                "-m",
                "camoudesk.core.browser_worker",
                "--profile-id",
                safe_id,
            ]
            if headless:
                cmd.append("--headless")
            if url_override:
                cmd.extend(["--url", url_override])

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=_build_worker_env(),
                    start_new_session=True,
                )
            except Exception as exc:
                log_fh.close()
                return {"ok": False, "error": f"Failed to start worker: {exc}"}

            self._processes[safe_id] = proc
            self._log_handles[safe_id] = log_fh
            return {"ok": True, "pid": proc.pid, "already_running": False}

    def stop(self, profile_id: str, timeout: float = 4.0) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            proc = self._processes.pop(safe_id, None)
            handle = self._log_handles.pop(safe_id, None)

        if proc is None:
            return {"ok": True, "was_running": False}

        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            with self._lock:
                self._last_errors.pop(safe_id, None)
        return {"ok": True, "was_running": True}

    def stop_all(self) -> None:
        with self._lock:
            active_ids = list(self._processes.keys())
        for pid_key in active_ids:
            self.stop(pid_key)

    def read_logs(self, profile_id: str, max_lines: int = 80) -> str:
        safe_id = sanitize_profile_id(profile_id)
        log_path = self.store.log_file(safe_id)
        if not log_path.is_file():
            return ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
                lines = stream.read().splitlines()
            return "\n".join(lines[-max_lines:])
        except OSError:
            return ""
