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
import re
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, Dict, Iterable, List, Optional, Tuple

from camoudesk.core.launcher import Network, build_launch_spec, minimal_env, resolve_network
from camoudesk.core.proxy import check_proxy_config
from camoudesk.core.proxy_forwarder import ProxyForwarder
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.storage import ProfileStore, RegistryCorruptError
from camoudesk.models.profile import Profile, ProxyConfig, sanitize_profile_id

MODE_DIRECT = "direct"
MODE_WORKER = "worker"
MODE_SDK = "sdk"  # a user script using camoudesk.sdk; tracked, never launched by the manager
_MODES = (MODE_DIRECT, MODE_WORKER)


def _build_worker_env() -> Dict[str, str]:
    """Minimal environment for the worker (and the browser it starts) that can import camoudesk.

    Secrets in the GUI's environment never reach workers or browsers; only the allowlist,
    CamouDesk's own settings and Python's search path are passed on.
    """
    env = minimal_env(extra_prefixes=("CAMOUDESK_", "PYTHONPATH", "PYTHONUTF8"))
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
        proxies: Optional[ProxyStore] = None,
    ) -> None:
        self.store = store
        self.proxies = proxies or ProxyStore(store.app_dir)
        self.stop_timeout = 10.0  # grace period for a browser to quit before signals escalate
        self.python_executable = python_executable or sys.executable
        self._lock = threading.Lock()
        self._runs: Dict[str, _Run] = {}
        self._last_errors: Dict[str, str] = {}
        self._warnings: Dict[str, str] = {}
        self._transitions: Dict[str, str] = {}  # profile_id -> "starting" | "stopping"
        self._listeners: List[Callable[[], None]] = []
        self._dirty = False
        self._monitor: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        if adopt_running:
            self._adopt_running()

    # ------------------------------------------------------------------ change notification

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
        network: Optional[Network] = None,
    ) -> Tuple[List[str], Dict[str, str], str]:
        """Return (argv, env, log header) for a launch. Overridden in tests."""
        if mode == MODE_WORKER:
            # -B: never write .pyc files (inside the app bundle they would break its code seal).
            cmd = [self.python_executable, "-B", "-m", "camoudesk.core.browser_worker", "--profile-id", profile.id]
            if headless:
                cmd.append("--headless")
            if url_override:
                cmd.extend(["--url", url_override])
            return cmd, _build_worker_env(), ""
        spec = build_launch_spec(
            profile, self.store, headless=headless, url_override=url_override, local_proxy=local_proxy,
            network=network or resolve_network(profile, self.proxies),
        )
        header = (
            f"[INFO] Launching Camoufox directly profile={profile.id} os={profile.os} "
            f"headless={headless} geoip={'on' if spec.geoip else 'off'}\n"
        )
        if local_proxy is not None:
            header += f"[INFO] Proxy credentials handled by local forwarder on 127.0.0.1:{local_proxy.port}\n"
        net = network or resolve_network(profile, self.proxies)
        header += f"[INFO] Network: proxy={net.source} timezone={net.timezone or 'host'} locale={net.locale or 'default'}\n"
        if net.proxy.is_configured and not spec.geoip:
            header += "[INFO] Geolocation/WebRTC not matched to the proxy IP (install the GeoIP database in Settings).\n"
        return [spec.executable, *spec.args], spec.env, header

    def launch(
        self,
        profile_id: str,
        headless: bool = False,
        url_override: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(profile_id)
        with self._lock:
            self._reap_finished_locked()
            current = self._transitions.get(safe_id)
            if current is None and safe_id in self._runs:
                run = self._runs[safe_id]
                return {"ok": True, "pid": run.pid, "mode": run.mode, "already_running": True}
            if current == "stopping":
                return {"ok": False, "error": "This profile is still shutting down; try again in a moment."}
            if current == "busy":
                return {"ok": False, "error": "This profile is busy (importing cookies); try again in a moment."}
            if current == "starting":
                return {"ok": True, "pid": None, "mode": None, "already_running": True}
            self._transitions[safe_id] = "starting"
            self._warnings.pop(safe_id, None)
        self._notify()
        try:
            return self._launch_impl(safe_id, headless, url_override, mode)
        finally:
            with self._lock:
                self._transitions.pop(safe_id, None)
            self._notify()

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

    def _check_and_resolve(self, profile: Profile, network: Network) -> Network:
        """First launch of a proxy profile with "auto" tz/locale: learn the exit location now."""
        result = check_proxy_config(network.proxy, timeout=8.0)
        if result.get("ok"):
            if network.source == "saved":
                self.proxies.record_check(network.proxy_id, result)
            else:
                self.store.record_inline_check(profile.id, result)
            fresh = self.store.get_profile(profile.id) or profile
            return resolve_network(fresh, self.proxies)
        self._proxy_logger(profile.id)(
            f"Proxy check failed ({result.get('error', 'unknown error')}); timezone/locale could not be "
            "matched to the proxy. The browser still routes only through the proxy."
        )
        return network

    def _launch_impl(
        self,
        safe_id: str,
        headless: bool,
        url_override: Optional[str],
        mode: Optional[str],
    ) -> Dict[str, Any]:
        profile = self.store.get_profile(safe_id)
        if profile is None:
            return {"ok": False, "error": f"Profile '{safe_id}' does not exist"}
        if profile.deleted_at:
            return {"ok": False, "error": "This profile is in the trash; restore it first."}

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

        network = resolve_network(profile, self.proxies)
        if network.missing_saved_proxy:
            return {"ok": False, "error": "This profile's saved proxy was deleted; choose another proxy or Direct."}
        if network.needs_check:
            network = self._check_and_resolve(profile, network)

        forwarder: Optional[ProxyForwarder] = None
        if chosen == MODE_DIRECT and network.proxy.has_credentials:
            try:
                forwarder = self._start_forwarder(profile, network.proxy)
            except Exception as exc:
                return {"ok": False, "error": f"Could not start the local proxy forwarder: {exc}"}

        # Computing the identity can take a moment; do it outside the lock.
        try:
            argv, env, header = self._build_command(
                profile, chosen, headless, url_override, forwarder.local_proxy if forwarder else None, network
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
