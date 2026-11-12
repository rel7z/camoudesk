"""Unit and live loopback integration tests for persistent browser_worker."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import pytest

from camoudesk.core.browser_worker import build_camoufox_kwargs
from camoudesk.core.storage import ProfileStore


class TestBrowserWorkerUnit(unittest.TestCase):
    def test_build_camoufox_kwargs_uses_persistent_context_and_fingerprint(self) -> None:
        fp = {"navigator": {"userAgent": "Mozilla/5.0 Firefox/146.0", "platform": "MacIntel"}}
        kwargs = build_camoufox_kwargs(
            profile_os="macos",
            user_data_dir="/tmp/ff_data",
            saved_fingerprint=fp,
            proxy_url="http://user:pass@127.0.0.1:8080",
            timezone="America/New_York",
            locale="en-US",
            block_webrtc=True,
            humanize=True,
            headless=False,
        )
        self.assertTrue(kwargs["persistent_context"])
        self.assertEqual(kwargs["user_data_dir"], "/tmp/ff_data")
        self.assertEqual(kwargs["fingerprint"], fp)
        self.assertTrue(kwargs["i_know_what_im_doing"])
        self.assertTrue(kwargs["canvas_noise"])
        self.assertEqual(kwargs["config"], {"timezone": "America/New_York"})
        self.assertEqual(
            kwargs["proxy"],
            {"server": "http://127.0.0.1:8080", "username": "user", "password": "pass"},
        )


@pytest.mark.live_browser
class TestBrowserWorkerLiveLoopback(unittest.TestCase):
    """Verifies the stdin=DEVNULL EOF auto-close bug is resolved and cookies persist."""

    def test_worker_stays_alive_with_devnull_and_persists_cookies(self) -> None:
        observed_cookies: list[str] = []

        class CookieTestHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/set":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Set-Cookie", "camoudesk_id=persisted_ok; Path=/; Max-Age=3600")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Cookie Set</body></html>")
                    return
                if self.path == "/check":
                    observed_cookies.append(self.headers.get("Cookie", ""))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Checked</body></html>")
                    return
                self.send_response(404)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), CookieTestHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        tmp = tempfile.TemporaryDirectory()
        try:
            app_dir = Path(tmp.name) / "app"
            legacy_dir = Path(tmp.name) / "legacy"
            store = ProfileStore(app_dir=app_dir, legacy_dir=legacy_dir)
            prof = store.create_profile(
                {
                    "name": "Live Cookie Check",
                    "os": "macos",
                    "startup_url": f"http://127.0.0.1:{port}/set",
                }
            )

            env = dict(os.environ)
            env["CAMOUDESK_HOME"] = str(app_dir)
            env["CAMOUDESK_LEGACY_HOME"] = str(legacy_dir)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

            # Launch 1: Spawn with stdin=DEVNULL; must stay alive >= 1.5s without immediate EOF exit!
            t0 = time.monotonic()
            proc1 = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "camoudesk.core.browser_worker",
                    "--profile-id",
                    prof.id,
                    "--headless",
                    "--max-seconds",
                    "1.5",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            elapsed1 = time.monotonic() - t0
            self.assertEqual(proc1.returncode, 0, msg=f"stderr: {proc1.stderr}")
            self.assertGreaterEqual(elapsed1, 1.4, msg="Browser exited too early (EOF regression!)")

            ff_dir = store.firefox_data_dir(prof.id)
            self.assertTrue(any(ff_dir.iterdir()), "Persistent firefox_data directory remained empty")

            # Launch 2: Visit /check on a fresh process and confirm cookie persisted on disk
            proc2 = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "camoudesk.core.browser_worker",
                    "--profile-id",
                    prof.id,
                    "--headless",
                    "--url",
                    f"http://127.0.0.1:{port}/check",
                    "--max-seconds",
                    "1.0",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc2.returncode, 0, msg=f"stderr: {proc2.stderr}")
            self.assertTrue(
                any("camoudesk_id=persisted_ok" in c for c in observed_cookies),
                f"Persisted cookie not sent back; observed={observed_cookies!r}",
            )
        finally:
            server.shutdown()
            server.server_close()
            tmp.cleanup()
