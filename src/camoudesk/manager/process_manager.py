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

import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from camoudesk.core.launcher import Network, ensure_location, override_urls, resolve_network
from camoudesk.core.proxy_forwarder import ProxyForwarder
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.constants import _MODES, MODE_DIRECT, MODE_SDK, MODE_WORKER, _build_worker_env  # noqa: F401
from camoudesk.manager.forwarders import ForwarderMixin
from camoudesk.manager.monitor import MonitorMixin
from camoudesk.manager.runs import RunRegistryMixin, _group_alive, _pid_alive, _Run, process_start_time  # noqa: F401
from camoudesk.manager.stopper import StopperMixin, _request_app_quit  # noqa: F401
from camoudesk.manager.tasks import TaskMixin
from camoudesk.models.profile import Profile, ProxyConfig, sanitize_profile_id


class ProcessManager(RunRegistryMixin, MonitorMixin, ForwarderMixin, StopperMixin, TaskMixin):
    """Tracks running browser processes per profile ID."""

    # Never exposed to the web page through pywebview's js_api (it walks nested objects).
    _serializable = False

    def __init__(
        self,
        store: ProfileStore,
        python_executable: Optional[str] = None,
        adopt_running: bool = True,
        proxies: Optional[ProxyStore] = None,
        engine: Any = None,
    ) -> None:
        from camoudesk.engines import get_engine

        self.store = store
        self.proxies = proxies or ProxyStore(store.app_dir)
        self.engine = engine or get_engine()  # the browser engine (engines/), for launch specs and host probes
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

    # ------------------------------------------------------------------ bookkeeping


    # ------------------------------------------------------------------ queries


    def default_mode(self, profile: Profile, headless: bool) -> str:
        # Headless runs are automation-shaped; interactive ones launch the browser directly.
        return MODE_WORKER if headless else MODE_DIRECT

    # ------------------------------------------------------------------ launch

    def _build_command(
        self,
        profile: Profile,
        mode: str,
        headless: bool,
        url_override: Any,
        local_proxy: Optional[ProxyConfig] = None,
        network: Optional[Network] = None,
    ) -> Tuple[List[str], Dict[str, str], str]:
        """Return (argv, env, log header) for a launch. Overridden in tests."""
        if mode == MODE_WORKER:
            # -B: never write .pyc files (inside the app bundle they would break its code seal).
            cmd = [self.python_executable, "-B", "-m", "camoudesk.core.browser_worker", "--profile-id", profile.id]
            if headless:
                cmd.append("--headless")
            for url in override_urls(url_override):
                cmd.extend(["--url", url])
            return cmd, _build_worker_env(), ""
        spec = self.engine.launch_spec(
            profile, self.store, network or resolve_network(profile, self.proxies),
            headless=headless, url_override=url_override, local_proxy=local_proxy,
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
        url_override: Any = None,
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


    def _check_and_resolve(self, profile: Profile, network: Network) -> Network:
        """First launch of a proxy profile with "auto" tz/locale: learn the exit location now."""
        return ensure_location(profile, network, self.store, self.proxies, self._proxy_logger(profile.id))

    def _launch_impl(
        self,
        safe_id: str,
        headless: bool,
        url_override: Any,
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
        if chosen == MODE_DIRECT:
            # The direct launch spec is computed here, in the GUI process: learn this Mac's GPU first
            # (once per Camoufox build) so a profile claiming this Mac can show its real canvas.
            self.engine.prepare_host(self.store.app_dir)
        if network.rotating:
            self._proxy_logger(profile.id)(
                "Rotating proxy session: the exit IP changes on every request, so sites may see this "
                "profile jump between IPs. Use a sticky or hard session for logins."
            )

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

    # ------------------------------------------------------------------ logs

