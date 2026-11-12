"""Browser process lifecycle: direct/worker launch, re-adoption after restart, logs, graceful stop.

Modes
-----
``direct`` (default for interactive use)
    Camoufox's binary is started straight from the GUI process with its identity passed through
    the environment. One process group per profile; no Python worker or Playwright driver.
    Proxies with credentials are served through a loopback ``ProxyForwarder`` in this process,
    because Firefox cannot authenticate to them itself.
``worker``
    The Playwright-based ``camoudesk.core.browser_worker``, used for headless/automation runs.

Every launch records ``profiles/<id>/run.json`` (pid, start time, forwarder port; never
credentials) so a restarted GUI re-adopts browsers that are still running, restarting their
forwarder on the same port, instead of launching a second instance onto the same profile.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, Dict, Iterable, List, Optional, Tuple

from camoudesk.core.launcher import build_launch_spec, has_proxy_credentials
from camoudesk.core.proxy_forwarder import ProxyForwarder
from camoudesk.core.storage import ProfileStore, RegistryCorruptError
from camoudesk.models.profile import Profile, ProxyConfig, sanitize_profile_id

MODE_DIRECT = "direct"
MODE_WORKER = "worker"
_MODES = (MODE_DIRECT, MODE_WORKER)


def _build_worker_env() -> Dict[str, str]:
    """Ensure child worker inherits current environment and can import camoudesk."""
    env = dict(os.environ)
    src_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if src_root not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src_root, *parts])
    return env


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


class ProcessManager:
    """Tracks running browser processes per profile ID."""

    def __init__(
        self,
        store: ProfileStore,
        python_executable: Optional[str] = None,
        adopt_running: bool = True,
    ) -> None:
        self.store = store
        self.python_executable = python_executable or sys.executable
        self._lock = threading.Lock()
        self._runs: Dict[str, _Run] = {}
        self._last_errors: Dict[str, str] = {}
        if adopt_running:
            self._adopt_running()

    # ------------------------------------------------------------------ bookkeeping

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

    def _proxy_logger(self, profile_id: str) -> Callable[[str], None]:
        """Append forwarder diagnostics (never credentials) to the profile's log."""
        log_path = self.store.log_file(profile_id)

        def write(message: str) -> None:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"[PROXY] {message}\n")
            except OSError:
                pass

        return write

    def _start_forwarder(self, profile: Profile, port: int = 0) -> ProxyForwarder:
        forwarder = ProxyForwarder(ProxyConfig.from_url(profile.proxy), port=port, on_error=self._proxy_logger(profile.id))
        forwarder.start()
        return forwarder

    def _adopt_running(self) -> None:
        """Re-attach to browsers recorded in run.json that are still the same live process."""
        try:
            profiles = self.store.list_profiles()
        except RegistryCorruptError:
            return
        for profile in profiles:
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
                self._clear_run_record(profile.id)
                continue
            self._runs[profile.id] = run
            if run.forward_port and has_proxy_credentials(profile):
                # The forwarder died with the previous GUI process; the browser's prefs still
                # point at this port, so bring it back there to restore its network access.
                try:
                    run.forwarder = self._start_forwarder(profile, port=run.forward_port)
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
            code = run.exit_code()
            if code not in (None, 0):
                tail = self.read_logs(profile_id, max_lines=10)
                self._last_errors[profile_id] = tail or f"Browser exited with status {code}"

    # ------------------------------------------------------------------ queries

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
            if run is not None:
                return {"status": "running", "pid": run.pid, "mode": run.mode, "last_error": ""}
            return {"status": "stopped", "pid": None, "mode": None, "last_error": self._last_errors.get(safe_id, "")}

    def default_mode(self, profile: Profile, headless: bool) -> str:
        # Headless runs are automation-shaped; interactive ones launch the browser directly.
        return MODE_WORKER if headless else MODE_DIRECT

    # ------------------------------------------------------------------ launch

    def _build_command(
        self,
        profile: Profile,
        mode: str,
        headless: bool,
        url_override: Optional[str],
        local_proxy: Optional[ProxyConfig] = None,
    ) -> Tuple[List[str], Dict[str, str], str]:
        """Return (argv, env, log header) for a launch. Overridden in tests."""
        if mode == MODE_WORKER:
            cmd = [self.python_executable, "-m", "camoudesk.core.browser_worker", "--profile-id", profile.id]
            if headless:
                cmd.append("--headless")
            if url_override:
                cmd.extend(["--url", url_override])
            return cmd, _build_worker_env(), ""
        spec = build_launch_spec(
            profile, self.store, headless=headless, url_override=url_override, local_proxy=local_proxy
        )
        header = (
            f"[INFO] Launching Camoufox directly profile={profile.id} os={profile.os} "
            f"headless={headless} geoip={'on' if spec.geoip else 'off'}\n"
        )
        if local_proxy is not None:
            header += f"[INFO] Proxy credentials handled by local forwarder on 127.0.0.1:{local_proxy.port}\n"
        if profile.proxy and not spec.geoip:
            header += "[INFO] Geolocation not matched to proxy (run 'Test proxy' and install camoufox[geoip]).\n"
        return [spec.executable, *spec.args], spec.env, header

    def launch(
        self,
        profile_id: str,
        headless: bool = False,
        url_override: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        profile = self.store.get_profile(safe_id)
        if profile is None:
            return {"ok": False, "error": f"Profile '{safe_id}' does not exist"}

        chosen = mode or self.default_mode(profile, headless)
        if chosen not in _MODES:
            return {"ok": False, "error": f"Unknown launch mode '{chosen}'"}

        with self._lock:
            self._reap_finished_locked()
            if safe_id in self._runs:
                run = self._runs[safe_id]
                return {"ok": True, "pid": run.pid, "mode": run.mode, "already_running": True}

        log_path = self.store.log_file(safe_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate, then append: the browser inherits an O_APPEND descriptor, so its output and
        # forwarder diagnostics written from this process interleave instead of overwriting.
        open(log_path, "w", encoding="utf-8").close()

        forwarder: Optional[ProxyForwarder] = None
        if chosen == MODE_DIRECT and has_proxy_credentials(profile):
            try:
                forwarder = self._start_forwarder(profile)
            except Exception as exc:
                return {"ok": False, "error": f"Could not start the local proxy forwarder: {exc}"}

        # Computing the identity can take a moment; do it outside the lock.
        try:
            argv, env, header = self._build_command(
                profile, chosen, headless, url_override, forwarder.local_proxy if forwarder else None
            )
        except Exception as exc:
            if forwarder is not None:
                forwarder.stop()
            return {"ok": False, "error": f"Could not prepare browser launch: {exc}"}

        with self._lock:
            self._reap_finished_locked()
            if safe_id in self._runs:
                if forwarder is not None:
                    forwarder.stop()
                run = self._runs[safe_id]
                return {"ok": True, "pid": run.pid, "mode": run.mode, "already_running": True}

            self._last_errors.pop(safe_id, None)
            log_fh = open(log_path, "a", encoding="utf-8")
            if header:
                log_fh.write(header)
                log_fh.flush()
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
            except Exception as exc:
                log_fh.close()
                if forwarder is not None:
                    forwarder.stop()
                return {"ok": False, "error": f"Failed to start browser: {exc}"}

            run = _Run(
                pid=proc.pid,
                pgid=proc.pid,  # start_new_session makes the child its own group leader
                mode=chosen,
                started=process_start_time(proc.pid),
                proc=proc,
                log_fh=log_fh,
                forward_port=forwarder.port if forwarder else 0,
                forwarder=forwarder,
            )
            self._runs[safe_id] = run
            self._write_run_record(safe_id, run)

        if chosen == MODE_DIRECT:
            # The worker marks itself; in direct mode nobody else will.
            try:
                self.store.mark_used(safe_id)
            except Exception:
                pass
        return {"ok": True, "pid": run.pid, "mode": chosen, "already_running": False}

    # ------------------------------------------------------------------ stop

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

        for run in pending:
            self._signal(run, signal.SIGTERM, group=True)
        pending = self._wait_gone(pending, 2.0)

        for run in pending:
            self._signal(run, signal.SIGKILL, group=True)
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

    def stop_all(self, timeout: float = 10.0) -> None:
        """Stop every browser in parallel so quitting the GUI waits at most ~timeout, not N*timeout."""
        with self._lock:
            self._reap_finished_locked()
            items = list(self._runs.items())
            self._runs.clear()
        survivors = set(id(r) for r in self._terminate([r for _, r in items], timeout))
        with self._lock:
            for profile_id, run in items:
                if id(run) in survivors:
                    self._runs[profile_id] = run
                else:
                    self._finish_locked(profile_id, run)

    # ------------------------------------------------------------------ logs

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
