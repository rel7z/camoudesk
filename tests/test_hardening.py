"""v0.2.1 hardening: single instance, SDK-safe quit, parallel stops, probes, running edits, CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import MODE_SDK, MODE_WORKER, process_start_time
from camoudesk.ui.api import DesktopBridgeApi
from camoudesk.ui.single_instance import InstanceLock
from test_api_and_manager import FakeBrowserManager, _group_alive

SRC = str(Path(__file__).resolve().parents[1] / "src")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=self.root / "app", legacy_dir=self.root / "legacy")
        self.proxies = ProxyStore(self.store.app_dir)
        self.manager = FakeBrowserManager(store=self.store, proxies=self.proxies)
        self.api = DesktopBridgeApi(self.store, self.manager, self.proxies, SettingsStore(self.store.app_dir))

    def tearDown(self) -> None:
        self.manager.stop_all(timeout=1.0, include_sdk=True)
        self.tmp.cleanup()


class TestSingleInstance(Case):
    def try_in_subprocess(self) -> dict:
        code = (
            f"import sys, json; sys.path.insert(0, {SRC!r})\n"
            "from camoudesk.ui.single_instance import InstanceLock\n"
            f"lock = InstanceLock({str(self.store.app_dir)!r})\n"
            "print(json.dumps({'acquired': lock.acquire(), 'holder': lock.holder_pid()}))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout)

    def test_second_instance_is_refused_until_release(self) -> None:
        lock = InstanceLock(self.store.app_dir)
        self.assertTrue(lock.acquire())
        other = self.try_in_subprocess()
        self.assertEqual(other, {"acquired": False, "holder": os.getpid()})
        lock.release()
        self.assertTrue(self.try_in_subprocess()["acquired"])


class TestStops(Case):
    def test_quitting_leaves_sdk_scripts_alone(self) -> None:
        prof = self.store.create_profile({"name": "Script"})
        script = subprocess.Popen(["sleep", "30"])
        threading.Thread(target=script.wait, daemon=True).start()
        try:
            self.store.run_file(prof.id).write_text(json.dumps(
                {"pid": script.pid, "pgid": script.pid, "mode": MODE_SDK, "started": process_start_time(script.pid)}))
            self.manager._adopt_running(only_new=True)
            self.manager.stop_all(timeout=1.0)  # what quitting the app does
            time.sleep(0.3)
            self.assertIsNone(script.poll(), "the user's script must survive the app quitting")
            self.assertTrue(self.manager.stop(prof.id, timeout=2)["exited"])  # a manual Stop still works
        finally:
            if script.poll() is None:
                script.kill()

    def test_trash_stops_running_profiles_in_parallel(self) -> None:
        self.manager.script = "trap '' TERM; sleep 30"  # ignores SIGTERM: full escalation each
        ids = [self.store.create_profile({"name": f"P{i}"}).id for i in range(3)]
        pids = [self.manager.launch(i, mode=MODE_WORKER)["pid"] for i in ids]
        time.sleep(0.2)
        self.manager.stop_timeout = 1.0  # profiles_trash uses the manager's grace period
        t0 = time.monotonic()
        res = self.api.profiles_trash(ids)["data"]
        elapsed = time.monotonic() - t0
        self.assertEqual(res, {"trashed": 3})
        self.assertFalse(any(_group_alive(p) for p in pids))
        self.assertLess(elapsed, 8, "stops must overlap, not run one after another (~15s)")


class TestRunningEditsAndSmallFixes(Case):
    def test_labels_editable_while_running_everything_else_locked(self) -> None:
        pid = self.store.create_profile({"name": "Live"}).id
        self.manager.launch(pid)
        ok = self.api.profiles_update(pid, {"name": "Renamed", "notes": "n", "folder": "F", "tags": ["t"]})
        self.assertTrue(ok["ok"], ok)
        self.assertEqual((ok["data"]["name"], ok["data"]["folder"], ok["data"]["tags"]), ("Renamed", "F", ["t"]))
        for locked in ({"os": "linux"}, {"proxy_mode": "none"}, {"timezone": "UTC"}, {"name": "x", "start_urls": ["https://a.io"]}):
            with self.subTest(payload=locked):
                res = self.api.profiles_update(pid, locked)
                self.assertFalse(res["ok"])
                self.assertIn("Stop the running browser", res["error"])
        self.assertEqual(self.store.get_profile(pid).name, "Renamed")  # rejected update changed nothing

    def test_cli_list_hides_trash_and_proxy_secrets(self) -> None:
        keep = self.store.create_profile({"name": "Keep", "folder": "Shops", "proxy": "http://u:s3cret@10.0.0.1:8080"})
        gone = self.store.create_profile({"name": "Gone"})
        self.store.set_trashed(gone.id, True)
        env = {**os.environ, "CAMOUDESK_HOME": str(self.store.app_dir), "CAMOUDESK_LEGACY_HOME": str(self.root / "legacy"),
               "PYTHONPATH": SRC}
        out = subprocess.run([sys.executable, "-m", "camoudesk.cli", "list"], capture_output=True, text=True, env=env, timeout=60)
        self.assertIn("[Shops] Keep", out.stdout)
        self.assertNotIn("Gone", out.stdout)
        js = subprocess.run([sys.executable, "-m", "camoudesk.cli", "list", "--json"], capture_output=True, text=True, env=env, timeout=60)
        self.assertNotIn("s3cret", js.stdout)
        self.assertIn(keep.id, js.stdout)

    def test_duplicate_skips_leftover_cookie_import_file(self) -> None:
        src = self.store.create_profile({"name": "Src"})
        (self.store.profile_dir(src.id) / ".cookie-import.json").write_text("[]")
        dup = self.store.clone_profile(src.id, copy_data=True)
        self.assertFalse((self.store.profile_dir(dup.id) / ".cookie-import.json").exists())


class _Resp:
    def __init__(self, status, payload):  # type: ignore[no-untyped-def]
        self.status_code, self._payload = status, payload

    def json(self):  # type: ignore[no-untyped-def]
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class TestProxyProbes(unittest.TestCase):
    IPINFO = {"ip": "203.0.113.9", "city": "Austin", "country": "US", "timezone": "America/Chicago"}
    IPAPI = {"status": "success", "query": "203.0.113.9", "country": "United States", "countryCode": "US",
             "city": "Austin", "timezone": "America/Chicago"}

    def run_probe(self, *responses):  # type: ignore[no-untyped-def]
        from camoudesk.core.proxy import check_proxy_health

        calls = []

        def fake_get(url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            r = responses[len(calls) - 1]
            if isinstance(r, Exception):
                raise r
            return r

        return check_proxy_health("http://u:p@10.0.0.1:8080", http_get=fake_get), calls

    def test_https_probe_first_and_normalised(self) -> None:
        res, calls = self.run_probe(_Resp(200, self.IPINFO))
        self.assertTrue(calls[0].startswith("https://"))
        self.assertEqual((res["ok"], res["country_code"], res["country"], res["timezone"]), (True, "US", "", "America/Chicago"))

    def test_rate_limit_or_bad_answer_falls_back(self) -> None:
        for first in (_Resp(429, {}), _Resp(200, ValueError("not json")), _Resp(200, {"bogon": True, "ip": "10.0.0.1"})):
            with self.subTest(first=first.status_code):
                res, calls = self.run_probe(first, _Resp(200, self.IPAPI))
                self.assertEqual(len(calls), 2)
                self.assertEqual((res["ok"], res["country"], res["country_code"]), (True, "United States", "US"))

    def test_dead_proxy_fails_without_trying_other_services(self) -> None:
        res, calls = self.run_probe(ConnectionError("Connection refused"))
        self.assertEqual(len(calls), 1)
        self.assertFalse(res["ok"])
        self.assertIn("refused", res["error"])

    def test_all_services_failing_reports_last_reason(self) -> None:
        res, _ = self.run_probe(_Resp(429, {}), _Resp(429, {}))
        self.assertFalse(res["ok"])
        self.assertIn("rate limited", res["error"])


if __name__ == "__main__":
    unittest.main()
