"""Phase 8: local REST API and Python SDK."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pytest

from camoudesk.core import api_server
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import MODE_SDK, ProcessManager, process_start_time
from camoudesk.ui.api import DesktopBridgeApi
from test_api_and_manager import FakeBrowserManager


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class AutomationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.proxies = ProxyStore(self.store.app_dir)
        self.settings = SettingsStore(self.store.app_dir)
        self.manager = FakeBrowserManager(store=self.store, proxies=self.proxies)
        self.bridge = DesktopBridgeApi(self.store, self.manager, self.proxies, self.settings)

    def tearDown(self) -> None:
        self.bridge.shutdown_api_server()
        self.manager.stop_all(timeout=1.0)
        self.tmp.cleanup()


class TestRestApi(AutomationCase):
    def setUp(self) -> None:
        super().setUp()
        self.port = _free_port()
        self.bridge.settings_update({"api": {"enabled": True, "port": self.port}})
        self.token = self.settings.get()["api"]["token"]

    def req(self, method, path, body=None, token="__default__", host=None):  # type: ignore[no-untyped-def]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        tok = self.token if token == "__default__" else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, payload

    def test_security_checks(self) -> None:
        self.assertEqual(self.req("GET", "/v1/profiles", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/v1/profiles", token="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/v1/profiles", host="evil.example")[0], 403)  # DNS rebinding
        self.assertEqual(self.req("GET", "/v1/nope")[0], 404)
        self.assertEqual(self.req("DELETE", "/v1/profiles")[0], 405)
        self.assertEqual(self.req("POST", "/v1/profiles", body=b"{not json")[0], 400)
        # Declare an oversized body without uploading it: the server must refuse from the header
        # alone (sending the full body would race the server closing the socket).
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/v1/profiles", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Authorization", f"Bearer {self.token}")
        conn.putheader("Content-Length", str(api_server.MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()
        status, body = self.req("GET", "/v1/health")
        self.assertEqual((status, body["ok"]), (200, True))

    def test_profile_lifecycle(self) -> None:
        status, created = self.req("POST", "/v1/profiles", {"name": "From API", "os": "linux", "tags": ["api"]})
        self.assertEqual((status, created["name"], created["os"]), (201, "From API", "linux"))
        pid = created["id"]
        self.assertIn(pid, [p["id"] for p in self.req("GET", "/v1/profiles")[1]])
        status, updated = self.req("PATCH", f"/v1/profiles/{pid}", {"notes": "via REST"})
        self.assertEqual((status, updated["notes"]), (200, "via REST"))
        status, started = self.req("POST", f"/v1/profiles/{pid}/start", {"headless": True})
        self.assertEqual((status, started["mode"]), (200, "worker"))
        self.assertEqual(self.req("GET", f"/v1/profiles/{pid}/status")[1]["status"], "running")
        self.assertEqual(self.req("POST", f"/v1/profiles/{pid}/stop")[0], 200)
        deadline = time.monotonic() + 8
        while self.req("GET", f"/v1/profiles/{pid}/status")[1]["status"] != "stopped" and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.req("GET", f"/v1/profiles/{pid}/status")[1]["status"], "stopped")
        self.assertEqual(self.req("GET", "/v1/profiles/nope_1")[0], 404)
        self.assertEqual(self.req("POST", "/v1/profiles/nope_1/start")[0], 404)
        self.assertEqual(self.req("GET", "/v1/profiles/nope_1/status")[0], 404)

    def test_settings_toggle_starts_and_stops_server(self) -> None:
        self.assertTrue(self.bridge.api_status()["data"]["running"])
        self.bridge.settings_update({"api": {"enabled": False}})
        status = self.bridge.api_status()["data"]
        self.assertFalse(status["running"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()

    def test_port_in_use_is_reported_not_raised(self) -> None:
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        try:
            res = self.bridge.settings_update({"api": {"enabled": True, "port": blocker.getsockname()[1]}})
            self.assertTrue(res["ok"])
            status = self.bridge.api_status()["data"]
            self.assertFalse(status["running"])
            self.assertIn("Could not listen", status["error"])
        finally:
            blocker.close()


class _FakeContext:
    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.kwargs = kwargs

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        return False


class TestSdk(AutomationCase):
    def test_open_profile_registers_and_cleans_up(self) -> None:
        from camoudesk import sdk

        prof = self.store.create_profile({"name": "SDK", "fp_overrides": {"block_images": True}})
        with patch("camoufox.sync_api.Camoufox", _FakeContext):
            with sdk.open_profile(prof.id, headless=True, store=self.store, block_webgl=True) as ctx:
                rec = json.loads(self.store.run_file(prof.id).read_text())
                self.assertEqual((rec["pid"], rec["mode"]), (os.getpid(), MODE_SDK))
                self.assertTrue(ctx.kwargs["persistent_context"] and ctx.kwargs["headless"])
                self.assertTrue(ctx.kwargs["block_images"] and ctx.kwargs["block_webgl"])
                # A second opener (this very process counts as running) is refused.
                with self.assertRaises(sdk.ProfileBusyError):
                    with sdk.open_profile(prof.id, store=self.store):
                        pass
        self.assertFalse(self.store.run_file(prof.id).exists())
        self.assertIsNotNone(self.store.get_profile(prof.id).last_used_at)

    def test_refuses_trashed_and_unknown(self) -> None:
        from camoudesk import sdk

        prof = self.store.create_profile({"name": "Gone"})
        self.store.set_trashed(prof.id, True)
        with patch("camoufox.sync_api.Camoufox", _FakeContext):
            with self.assertRaises(sdk.ProfileBusyError):
                with sdk.open_profile(prof.id, store=self.store):
                    pass
            with self.assertRaises(KeyError):
                with sdk.open_profile("nope_1", store=self.store):
                    pass
        self.assertNotIn(prof.id, [p.id for p in sdk.list_profiles(self.store)])

    def test_manager_adopts_and_stops_sdk_run_by_pid_only(self) -> None:
        prof = self.store.create_profile({"name": "Script"})
        child = subprocess.Popen(["sleep", "30"])  # stands in for a user script (same process group as us)
        # A real script is not our child; reap this one like its own parent would, or it lingers
        # as a zombie that still looks alive.
        threading.Thread(target=child.wait, daemon=True).start()
        try:
            self.store.run_file(prof.id).write_text(json.dumps(
                {"pid": child.pid, "pgid": child.pid, "mode": MODE_SDK, "started": process_start_time(child.pid)}))
            manager = ProcessManager(store=self.store, proxies=self.proxies, adopt_running=False)
            manager._adopt_running(only_new=True)
            st = manager.get_status(prof.id)
            self.assertEqual((st["status"], st["mode"]), ("running", MODE_SDK))
            self.assertTrue(manager.stop(prof.id, timeout=3)["exited"])
            child.wait(5)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
        # Reaching this line proves our own process group was not signalled.
        self.assertEqual(manager.get_status(prof.id)["status"], "stopped")


@pytest.mark.live_browser
class TestSdkLive(unittest.TestCase):
    def test_sdk_drives_a_real_profile_and_keeps_cookies(self) -> None:
        from camoudesk import sdk

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                seen.append(self.headers.get("Cookie", ""))
                self.send_response(200)
                self.send_header("Set-Cookie", "sdk=1; Path=/; Max-Age=3600")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(app_dir=Path(tmp) / "app", legacy_dir=Path(tmp) / "legacy")
            prof = store.create_profile({"name": "SDK live", "os": "windows"})
            try:
                for _ in range(2):
                    with sdk.open_profile(prof.id, headless=True, store=store) as context:
                        page = context.new_page()
                        page.goto(url)
                        ua = page.evaluate("navigator.userAgent")
                self.assertIn("Windows", ua)  # the profile's own identity
                self.assertIn("sdk=1", seen[-1])  # cookie persisted between sessions
                self.assertFalse(store.run_file(prof.id).exists())
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
