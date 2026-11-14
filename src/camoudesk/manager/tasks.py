"""Short headless worker tasks (cookie import, anonymity check), cookies, logs and diagnostics.

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


class TaskMixin(ManagerState):

    def _begin_busy(self, safe_id: str) -> None:
        with self._lock:
            self._reap_finished_locked()
            if safe_id in self._runs:
                raise ValueError("Stop the profile first.")
            if self._transitions.get(safe_id):
                raise ValueError("This profile is busy; try again in a moment.")
            self._transitions[safe_id] = "busy"
        self._notify()


    def _end_busy(self, safe_id: str) -> None:
        with self._lock:
            self._transitions.pop(safe_id, None)
        self._notify()


    def import_cookies(self, profile_id: str, cookies: List[Dict[str, Any]], timeout: float = 180.0) -> Dict[str, Any]:
        """Add cookies through a short headless run of the profile (Firefox writes cookies.sqlite)."""
        safe_id = sanitize_profile_id(profile_id)
        if self.store.get_profile(safe_id) is None:
            raise KeyError("Profile not found")
        self._begin_busy(safe_id)
        tmp_path = self.store.profile_dir(safe_id) / ".cookie-import.json"
        try:
            tmp_path.write_text(json.dumps(cookies), encoding="utf-8")
            os.chmod(tmp_path, 0o600)
            cmd = self._cookie_import_command(safe_id, tmp_path)
            try:
                proc = subprocess.run(
                    cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=_build_worker_env()
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Cookie import timed out") from exc
            for line in proc.stdout.splitlines():
                if line.startswith("[RESULT] "):
                    return json.loads(line[len("[RESULT] "):])
            errors = [ln for ln in (proc.stderr + proc.stdout).splitlines() if ln.startswith("[ERROR]")]
            raise RuntimeError(errors[-1][len("[ERROR] "):] if errors else f"Cookie import failed (exit {proc.returncode})")
        finally:
            tmp_path.unlink(missing_ok=True)
            self._end_busy(safe_id)


    def check_identity(self, profile_id: str, timeout: float = 180.0) -> Dict[str, Any]:
        """Run the anonymity probe in a short headless run of the profile; returns its raw result."""
        safe_id = sanitize_profile_id(profile_id)
        if self.store.get_profile(safe_id) is None:
            raise KeyError("Profile not found")
        if self.is_running(safe_id):
            raise RuntimeError("Stop the browser first: the check opens the profile itself.")
        self._begin_busy(safe_id)
        try:
            cmd = [self.python_executable, "-B", "-m", "camoudesk.core.browser_worker", "--profile-id", safe_id, "--check-identity"]
            try:
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                                      env=_build_worker_env())
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("The anonymity check timed out") from exc
            for line in proc.stdout.splitlines():
                if line.startswith("[RESULT] "):
                    return json.loads(line[len("[RESULT] "):])
            errors = [ln for ln in (proc.stderr + proc.stdout).splitlines() if ln.startswith("[ERROR]")]
            raise RuntimeError(errors[-1][len("[ERROR] "):] if errors else f"The anonymity check failed (exit {proc.returncode})")
        finally:
            self._end_busy(safe_id)


    def _cookie_import_command(self, safe_id: str, cookie_file: Path) -> List[str]:
        return [self.python_executable, "-B", "-m", "camoudesk.core.browser_worker", "--profile-id", safe_id,
                "--import-cookies", str(cookie_file)]


    def clear_cookies(self, profile_id: str) -> None:
        safe_id = sanitize_profile_id(profile_id)
        self._begin_busy(safe_id)
        try:
            ff = self.store.firefox_data_dir(safe_id)
            for suffix in ("", "-wal", "-shm", "-journal"):
                (ff / f"cookies.sqlite{suffix}").unlink(missing_ok=True)
        finally:
            self._end_busy(safe_id)


    def diagnostics(self, profile_id: str) -> str:
        """Masked, copyable launch summary for bug reports (never contains proxy credentials)."""
        from camoudesk import __version__

        safe_id = sanitize_profile_id(profile_id)
        profile = self.store.get_profile(safe_id)
        status = self.get_status(safe_id)
        lines = [f"CamouDesk {__version__}", f"profile: {safe_id}"]
        if profile is None:
            lines.append("profile not found")
        else:
            net = resolve_network(profile, self.proxies)
            proxy = net.proxy
            with self._lock:
                run = self._runs.get(safe_id)
            local = None
            if proxy.has_credentials and run is not None and run.forward_port:
                local = ProxyConfig(scheme="socks5" if proxy.scheme.startswith("socks") else "http",
                                    host="127.0.0.1", port=run.forward_port)
            effective = (local or (proxy if not proxy.has_credentials else ProxyConfig())).to_firefox_prefs()
            lines += [
                f"os: {profile.os}  timezone: {profile.timezone}  locale: {profile.locale}",
                f"status: {status['status']}  mode: {status.get('mode')}  pid: {status.get('pid')}",
                f"proxy: {proxy.masked_display()}  type: {proxy.scheme or 'none'}  "
                f"credentials: {'yes' if proxy.has_credentials else 'no'}",
                f"forwarder port: {run.forward_port if run is not None and run.forward_port else '-'}",
                f"proxy source: {net.source}{' (MISSING saved proxy)' if net.missing_saved_proxy else ''}  "
                f"exit ip known: {'yes' if net.exit_ip else 'no'}  country: {net.country_code or '-'}",
                f"effective timezone: {net.timezone or 'host'}  effective locale: {net.locale or 'default'}",
                f"start urls: {', '.join(profile.start_urls) or '-'}",
                "effective proxy prefs: " + (json.dumps(effective, sort_keys=True) if effective else "none (system/direct)"),
                f"last error: {status.get('last_error') or '-'}",
                f"warning: {status.get('warning') or '-'}",
            ]
        log = self.read_logs(safe_id, max_lines=60)
        log = re.sub(r"://([^:@/\s]+):([^@/\s]+)@", r"://\1:***@", log)
        lines += ["--- log tail ---", log]
        return "\n".join(lines)


    def read_logs(self, profile_id: str, max_lines: int = 80) -> str:
        safe_id = sanitize_profile_id(profile_id)
        log_path = self.store.log_file(safe_id)
        if not log_path.is_file():
            return ""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as stream:
                lines = stream.read().splitlines()
            return "\n".join(lines[-max_lines:])
        except OSError:
            return ""

