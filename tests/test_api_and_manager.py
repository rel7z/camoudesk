"""Unit tests for ProcessManager and DesktopBridgeApi.

Browser launches are replaced with small real shell process groups so lifecycle code (process
groups, signal escalation, run.json adoption) is exercised against the OS rather than mocks.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple
import unittest
from unittest.mock import patch
import urllib.request

from proxy_fakes import FakeHttpProxy, OriginServer

from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import MODE_DIRECT, MODE_WORKER, ProcessManager
from camoudesk.models.profile import Profile, ProxyConfig
from camoudesk.ui.api import DesktopBridgeApi


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


class FakeBrowserManager(ProcessManager):
    """ProcessManager whose launches run a configurable shell script instead of a browser."""

    script = "sleep 30"

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.local_proxies: List[Optional[ProxyConfig]] = []
        self.networks: list = []
        super().__init__(*args, **kwargs)

    def _build_command(
        self,
        profile: Profile,
        mode: str,
        headless: bool,
        url_override: Optional[str],
        local_proxy: Optional[ProxyConfig] = None,
        network=None,  # type: ignore[no-untyped-def]
    ) -> Tuple[List[str], Dict[str, str], str]:
        self.local_proxies.append(local_proxy)
        self.networks.append(network)
        return ["/bin/sh", "-c", self.script], dict(os.environ), f"[INFO] fake {mode}\n"


class TestProcessManagerAndApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=self.root / "app", legacy_dir=self.root / "legacy")
        self.manager = FakeBrowserManager(store=self.store)
        self.api = DesktopBridgeApi(store=self.store, manager=self.manager)

    def tearDown(self) -> None:
        self.manager.stop_all(timeout=1.0)
        self.tmp.cleanup()

    def test_launch_logs_status_and_edit_guard(self) -> None:
        created = self.api.profiles_create({"name": "Worker Test", "os": "macos", "proxy": "http://u:secret@127.0.0.1:8888"})
        self.assertTrue(created["ok"])
        pid = created["data"]["id"]
        self.assertNotIn("secret", json.dumps(created["data"]))  # no password anywhere in the row
        self.assertTrue(created["data"]["proxy_parts"]["has_password"])

        res = self.api.profiles_launch(pid)
        self.assertTrue(res["ok"], res)
        launched = res["data"]
        self.assertEqual(launched["mode"], MODE_DIRECT)  # credentials are handled by the forwarder

        # Editing while running must be rejected safely
        self.assertFalse(self.api.profiles_update(pid, {"name": "Blocked Rename"})["ok"])

        row = next(r for r in self.api.profiles_list()["data"] if r["id"] == pid)
        self.assertEqual((row["status"], row["pid"], row["mode"]), ("running", launched["pid"], MODE_DIRECT))

        stopped = self.api.profiles_stop(pid)
        self.assertTrue(stopped["ok"] and stopped["data"]["stopping"])  # returns at once; stop runs in background
        self.assertTrue(self._wait_status(pid, "stopped"))
        self.assertFalse(_group_alive(launched["pid"]))

        edit_ok = self.api.profiles_update(pid, {"name": "Renamed Cleanly"})
        self.assertTrue(edit_ok["ok"])
        self.assertEqual(edit_ok["data"]["name"], "Renamed Cleanly")

    def test_worker_command_uses_devnull_stdin(self) -> None:
        real_popen = subprocess.Popen
        seen = {}

        def spy(argv, **kwargs):
            if argv and argv[0] == "ps":  # process_start_time() lookups go through untouched
                return real_popen(argv, **kwargs)
            seen.update(kwargs, argv=argv)
            return real_popen(["/bin/sh", "-c", "sleep 30"], **kwargs)

        manager = ProcessManager(store=self.store, adopt_running=False)
        try:
            prof_id = self.store.list_profiles()[0].id
            with patch("camoudesk.manager.process_manager.subprocess.Popen", side_effect=spy):
                res = manager.launch(prof_id, headless=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["mode"], MODE_WORKER)
            self.assertEqual(seen["stdin"], subprocess.DEVNULL)
            self.assertTrue(seen["start_new_session"])
            self.assertIn("camoudesk.core.browser_worker", seen["argv"])
        finally:
            manager.stop_all(timeout=1.0)

    def test_crashed_browser_surfaces_log_tail(self) -> None:
        self.manager.script = 'echo "[ERROR] Proxy connection refused"; exit 1'
        prof_id = self.store.list_profiles()[0].id
        self.assertTrue(self.api.profiles_launch(prof_id)["ok"])

        deadline = time.monotonic() + 5
        while self.manager.is_running(prof_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        st = self.manager.get_status(prof_id)
        self.assertEqual(st["status"], "stopped")
        self.assertIn("Proxy connection refused", st["last_error"])
        self.assertFalse(self.store.run_file(prof_id).exists())

        logs_res = self.api.profiles_logs(prof_id)
        self.assertTrue(logs_res["ok"])
        self.assertIn("Proxy connection refused", logs_res["data"])

    def test_restarted_manager_adopts_and_stops_whole_group(self) -> None:
        prof_id = self.store.list_profiles()[0].id
        # A separate "GUI" process launches a browser (with a grandchild, like Firefox's content
        # processes) and then crashes without cleanup, so the browser is reparented to launchd.
        crashed_gui = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
class M(ProcessManager):
    def _build_command(self, profile, mode, headless, url, local_proxy=None, network=None):
        return ["/bin/sh", "-c", "sleep 30 & sleep 30"], dict(os.environ), ""
store = ProfileStore(app_dir=Path({str(self.root / "app")!r}), legacy_dir=Path({str(self.root / "legacy")!r}))
print(M(store=store).launch({prof_id!r}, mode="direct")["pid"], flush=True)
os._exit(0)
"""
        out = subprocess.run([sys.executable, "-c", crashed_gui], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        browser_pid = int(out.stdout.strip())
        record = json.loads(self.store.run_file(prof_id).read_text())
        self.assertEqual((record["pid"], record["mode"]), (browser_pid, MODE_DIRECT))

        reborn = ProcessManager(store=self.store)
        self.assertEqual(reborn.get_status(prof_id)["status"], "running")
        self.assertEqual(reborn.launch(prof_id)["already_running"], True)

        stopped = reborn.stop(prof_id, timeout=1.0)
        self.assertTrue(stopped["exited"], stopped)
        self.assertFalse(_group_alive(browser_pid))
        self.assertFalse(self.store.run_file(prof_id).exists())

    def test_stale_run_record_is_discarded(self) -> None:
        prof_id = self.store.list_profiles()[0].id
        # Our own PID is alive but its start time does not match: a recycled PID must not be adopted.
        self.store.run_file(prof_id).write_text(
            json.dumps({"pid": os.getpid(), "pgid": os.getpid(), "mode": MODE_DIRECT, "started": "Thu Jan  1 00:00:00 1970"})
        )
        reborn = ProcessManager(store=self.store)
        self.assertEqual(reborn.get_status(prof_id)["status"], "stopped")
        self.assertFalse(self.store.run_file(prof_id).exists())

    def test_stop_escalates_to_sigkill_for_unresponsive_group(self) -> None:
        # Ignored signals stay ignored across exec, so neither sh nor sleep will honour SIGTERM.
        self.manager.script = "trap '' TERM; sleep 30"
        prof_id = self.store.list_profiles()[0].id
        res = self.manager.launch(prof_id, mode=MODE_WORKER)
        time.sleep(0.2)
        t0 = time.monotonic()
        stopped = self.manager.stop(prof_id, timeout=0.5)
        self.assertTrue(stopped["exited"], stopped)
        self.assertFalse(_group_alive(res["pid"]))
        self.assertLess(time.monotonic() - t0, 6)

    def test_stop_all_stops_in_parallel(self) -> None:
        self.manager.script = "trap '' TERM; sleep 30"
        ids = [self.store.create_profile({"name": f"p{i}"}).id for i in range(3)]
        pids = [self.manager.launch(i, mode=MODE_WORKER)["pid"] for i in ids]
        time.sleep(0.2)
        t0 = time.monotonic()
        self.manager.stop_all(timeout=1.0)
        elapsed = time.monotonic() - t0
        self.assertFalse(any(_group_alive(p) for p in pids))
        self.assertLess(elapsed, 6, "stop_all should not wait timeout x N sequentially")

    def _wait_status(self, profile_id: str, status: str, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.manager.get_status(profile_id)["status"] == status:
                return True
            time.sleep(0.05)
        return False

    def test_stop_async_reports_stopping_and_notifies(self) -> None:
        events: List[str] = []
        prof_id = self.store.list_profiles()[0].id
        self.manager.add_listener(lambda: events.append(self.manager.get_status(prof_id)["status"]))
        self.manager.script = "trap '' TERM; sleep 30"  # takes the full escalation path
        self.assertTrue(self.manager.launch(prof_id, mode=MODE_WORKER)["ok"])
        self.assertIn("running", events)

        res = self.manager.stop_async(prof_id, timeout=0.5)
        self.assertTrue(res["stopping"])
        self.assertEqual(self.manager.get_status(prof_id)["status"], "stopping")
        refused = self.manager.launch(prof_id, mode=MODE_WORKER)
        self.assertFalse(refused["ok"])
        self.assertIn("shutting down", refused["error"])

        self.assertTrue(self._wait_status(prof_id, "stopped"))
        self.assertIn("stopping", events)
        self.assertEqual(events[-1], "stopped")

    def test_monitor_notices_browser_exiting_on_its_own(self) -> None:
        fired = []
        self.manager.add_listener(lambda: fired.append(time.monotonic()))
        self.manager.script = "sleep 0.3"
        prof_id = self.store.list_profiles()[0].id
        self.manager.launch(prof_id, mode=MODE_WORKER)
        fired.clear()
        self.manager.start_monitor(interval=0.1)
        try:
            deadline = time.monotonic() + 5
            while not fired and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(fired, "listener should fire when the browser exits by itself")
            self.assertEqual(self.manager.get_status(prof_id)["status"], "stopped")
        finally:
            self.manager.stop_monitor()

    def test_failure_summary_ignores_firefox_noise(self) -> None:
        prof_id = self.store.list_profiles()[0].id
        self.manager.script = (
            'echo "Error in cpuinfo: sysctlbyname failed"; echo "JavaScript warning: noise"; exit 3'
        )
        self.manager.launch(prof_id, mode=MODE_WORKER)
        self.assertTrue(self._wait_status(prof_id, "stopped"))
        self.assertEqual(self.manager.get_status(prof_id)["last_error"], "Browser exited with status 3")

        self.manager.script = 'echo "[ERROR] Camoufox launch failed: boom"; echo "Error in cpuinfo: x"; exit 1'
        self.manager.launch(prof_id, mode=MODE_WORKER)
        self.assertTrue(self._wait_status(prof_id, "stopped"))
        self.assertEqual(self.manager.get_status(prof_id)["last_error"], "[ERROR] Camoufox launch failed: boom")

    def test_diagnostics_are_masked(self) -> None:
        origin = OriginServer()
        upstream = FakeHttpProxy(origin.port, "u", "diag-secret")
        try:
            prof = self.store.create_profile({"name": "Diag", "proxy": f"http://u:diag-secret@127.0.0.1:{upstream.port}"})
            self.manager.launch(prof.id)
            text = self.api.profiles_diagnostics(prof.id)["data"]
            self.assertNotIn("diag-secret", text)
            self.assertIn("credentials: yes", text)
            self.assertIn("network.proxy.http", text)  # the forwarder prefs Firefox actually uses
            self.assertIn(f"forwarder port: {self.manager.local_proxies[-1].port}", text)
        finally:
            self.manager.stop_all(timeout=1.0)
            upstream.close()
            origin.close()

    def test_worker_env_withholds_secrets(self) -> None:
        from camoudesk.manager.process_manager import _build_worker_env

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-secret", "CAMOUDESK_HOME": "/tmp/x", "HOME": "/Users/t"}):
            env = _build_worker_env()
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertEqual(env["CAMOUDESK_HOME"], "/tmp/x")
        self.assertEqual(env["HOME"], "/Users/t")
        self.assertIn("PYTHONPATH", env)

    def _port_open(self, port: int) -> bool:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return True
        except OSError:
            return False

    def test_credentialed_direct_launch_runs_forwarder(self) -> None:
        origin = OriginServer()
        upstream = FakeHttpProxy(origin.port, "u", "secret-pw")
        try:
            prof = self.store.create_profile({"name": "Authed", "proxy": f"http://u:secret-pw@127.0.0.1:{upstream.port}"})
            res = self.manager.launch(prof.id)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["mode"], MODE_DIRECT)
            local = self.manager.local_proxies[-1]
            self.assertIsNotNone(local)
            self.assertFalse(local.has_credentials)
            self.assertEqual(local.host, "127.0.0.1")

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": local.raw_url}))
            self.assertEqual(opener.open("http://origin.camoudesk.test/via", timeout=10).read(), b"hello /via")

            run_text = self.store.run_file(prof.id).read_text()
            self.assertEqual(json.loads(run_text)["forward_port"], local.port)
            self.assertNotIn("secret-pw", run_text)
            self.assertNotIn("secret-pw", self.manager.read_logs(prof.id))

            self.assertTrue(self.manager.stop(prof.id, timeout=1.0)["exited"])
            self.assertFalse(self._port_open(local.port))
        finally:
            upstream.close()
            origin.close()

    def test_forwarder_stops_when_browser_exits_on_its_own(self) -> None:
        self.manager.script = "sleep 0.3"
        prof = self.store.create_profile({"name": "Authed", "proxy": "socks5://u:p@127.0.0.1:1080"})
        self.assertTrue(self.manager.launch(prof.id)["ok"])
        port = self.manager.local_proxies[-1].port
        self.assertTrue(self._port_open(port))
        deadline = time.monotonic() + 5
        while self.manager.is_running(prof.id) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._port_open(port))

    def test_restarted_manager_restores_forwarder_on_recorded_port(self) -> None:
        origin = OriginServer()
        upstream = FakeHttpProxy(origin.port, "u", "pw")
        try:
            prof = self.store.create_profile({"name": "Authed", "proxy": f"http://u:pw@127.0.0.1:{upstream.port}"})
            crashed_gui = f"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
class M(ProcessManager):
    def _build_command(self, profile, mode, headless, url, local_proxy=None, network=None):
        return ["/bin/sh", "-c", "sleep 30"], dict(os.environ), ""
store = ProfileStore(app_dir=Path({str(self.root / "app")!r}), legacy_dir=Path({str(self.root / "legacy")!r}))
res = M(store=store).launch({prof.id!r})
print(json.dumps(res), flush=True)
os._exit(0)
"""
            out = subprocess.run([sys.executable, "-c", crashed_gui], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            launched = json.loads(out.stdout.strip())
            port = json.loads(self.store.run_file(prof.id).read_text())["forward_port"]
            self.assertFalse(self._port_open(port), "forwarder should have died with the crashed GUI")

            reborn = ProcessManager(store=self.store)
            self.assertEqual(reborn.get_status(prof.id)["status"], "running")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"}))
            self.assertEqual(opener.open("http://origin.camoudesk.test/back", timeout=10).read(), b"hello /back")

            self.assertTrue(reborn.stop(prof.id, timeout=1.0)["exited"])
            self.assertFalse(self._port_open(port))
            self.assertFalse(_group_alive(launched["pid"]))
        finally:
            upstream.close()
            origin.close()

    def test_delete_refused_while_browser_survives(self) -> None:
        prof = self.store.create_profile({"name": "Sticky"})
        with patch.object(self.manager, "stop", return_value={"ok": False, "exited": False}):
            res = self.api.profiles_delete(prof.id)
        self.assertFalse(res["ok"])
        self.assertTrue(self.store.profile_dir(prof.id).exists())
        self.assertIsNotNone(self.store.get_profile(prof.id))


if __name__ == "__main__":
    unittest.main()
