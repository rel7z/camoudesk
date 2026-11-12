"""Cookie import/export: parsers, sqlite reader, manager busy state, and a live round trip."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

import pytest

from camoudesk.core import cookies as C
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import MODE_DIRECT, ProcessManager
from test_api_and_manager import FakeBrowserManager

FUTURE = time.time() + 86400 * 30


class TestParsers(unittest.TestCase):
    def test_extension_json(self) -> None:
        text = json.dumps([
            {"domain": "example.com", "hostOnly": False, "name": "sid", "value": "abc", "path": "/", "secure": True,
             "httpOnly": True, "sameSite": "no_restriction", "expirationDate": FUTURE},
            {"domain": "shop.example.com", "hostOnly": True, "name": "cart", "value": "1", "session": True, "sameSite": "unspecified"},
            {"domain": "old.example", "name": "gone", "value": "x", "expirationDate": 1000},
        ])
        got = C.parse_cookies(text)
        self.assertEqual(len(got), 2)  # expired one dropped
        sid, cart = got
        self.assertEqual((sid["domain"], sid["sameSite"], sid["httpOnly"], round(sid["expires"])), (".example.com", "None", True, round(FUTURE)))
        self.assertEqual((cart["domain"], cart["expires"], cart["sameSite"]), ("shop.example.com", -1, "Lax"))

    def test_samesite_none_requires_secure(self) -> None:
        got = C.parse_cookies(json.dumps([{"domain": "a.io", "name": "n", "value": "v", "sameSite": "none", "secure": False}]))
        self.assertEqual(got[0]["sameSite"], "Lax")

    def test_playwright_storage_state_and_ms_expiry(self) -> None:
        state = {"cookies": [{"name": "t", "value": "1", "domain": ".a.io", "path": "/", "expires": FUTURE * 1000,
                              "httpOnly": False, "secure": True, "sameSite": "Strict"}], "origins": []}
        got = C.parse_cookies(json.dumps(state))[0]
        self.assertEqual((round(got["expires"]), got["sameSite"]), (round(FUTURE), "Strict"))

    def test_netscape(self) -> None:
        text = (
            "# Netscape HTTP Cookie File\n"
            f".example.com\tTRUE\t/\tTRUE\t{int(FUTURE)}\tsid\tabc\n"
            f"#HttpOnly_www.example.com\tFALSE\t/app\tFALSE\t0\tsess\tz\n"
        )
        a, b = C.parse_cookies(text)
        self.assertEqual((a["domain"], a["secure"], a["httpOnly"]), (".example.com", True, False))
        self.assertEqual((b["domain"], b["path"], b["httpOnly"], b["expires"]), ("www.example.com", "/app", True, -1))
        with self.assertRaises(C.CookieFormatError):
            C.parse_cookies("bad\tline")

    def test_errors(self) -> None:
        for bad in ("", "{not json", json.dumps({"x": 1}), json.dumps([{"name": "no-domain"}])):
            with self.subTest(bad=bad), self.assertRaises(C.CookieFormatError):
                C.parse_cookies(bad)

    def test_export_formats_round_trip(self) -> None:
        cookies = [{"domain": ".a.io", "name": "x", "value": "1", "path": "/", "expires": round(FUTURE), "secure": True,
                    "httpOnly": True, "sameSite": "Strict"},
                   {"domain": "b.io", "name": "y", "value": "2", "path": "/", "expires": -1, "secure": False,
                    "httpOnly": False, "sameSite": "Lax"}]
        self.assertEqual(C.parse_cookies(C.to_json(cookies)), cookies)
        back = C.parse_cookies(C.to_netscape(cookies))
        self.assertEqual([(c["domain"], c["name"], c["httpOnly"], c["expires"]) for c in back],
                         [(".a.io", "x", True, round(FUTURE)), ("b.io", "y", False, -1)])
        self.assertEqual(C.summarize(cookies + cookies[:1]), (3, [{"site": "a.io", "count": 2}, {"site": "b.io", "count": 1}]))


class TestSqliteReader(unittest.TestCase):
    def test_reads_seconds_and_milliseconds_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ff = Path(tmp)
            conn = sqlite3.connect(ff / "cookies.sqlite")
            conn.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT, name TEXT, value TEXT, host TEXT, "
                         "path TEXT, expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER, "
                         "inBrowserElement INTEGER, sameSite INTEGER, schemeMap INTEGER)")
            conn.execute("INSERT INTO moz_cookies (name,value,host,path,expiry,isSecure,isHttpOnly,sameSite) VALUES ('s','1','.a.io','/',?,1,1,2)", (int(FUTURE),))
            conn.execute("INSERT INTO moz_cookies (name,value,host,path,expiry,isSecure,isHttpOnly,sameSite) VALUES ('m','2','b.io','/',?,0,0,1)", (int(FUTURE * 1000),))
            conn.commit()
            conn.close()
            got = {c["name"]: c for c in C.read_profile_cookies(ff)}
            self.assertEqual(got["s"]["expires"], int(FUTURE))  # stored as whole seconds
            self.assertEqual(got["m"]["expires"], int(FUTURE * 1000) / 1000)  # stored as milliseconds
            self.assertEqual((got["s"]["sameSite"], got["m"]["sameSite"]), ("Strict", "Lax"))
            self.assertEqual(C.read_profile_cookies(ff / "missing"), [])


class TestManagerCookieOps(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.pid = self.store.list_profiles()[0].id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_import_is_busy_and_blocks_launch(self) -> None:
        seen = {}

        class SlowImport(FakeBrowserManager):
            def _cookie_import_command(self, safe_id, cookie_file):  # type: ignore[no-untyped-def]
                seen["file_mode"] = oct(os.stat(cookie_file).st_mode & 0o777)
                seen["status"] = self.get_status(safe_id)["status"]
                seen["launch"] = self.launch(safe_id)
                return [sys.executable, "-c", 'print("[RESULT] {\\"requested\\": 1, \\"before\\": 0, \\"after\\": 1}")']

        mgr = SlowImport(store=self.store)
        res = mgr.import_cookies(self.pid, [{"name": "a", "value": "b", "domain": ".x.io", "path": "/"}])
        self.assertEqual(res["after"], 1)
        self.assertEqual(seen["status"], "busy")
        self.assertEqual(seen["file_mode"], "0o600")
        self.assertFalse(seen["launch"]["ok"])
        self.assertEqual(mgr.get_status(self.pid)["status"], "stopped")
        self.assertFalse((self.store.profile_dir(self.pid) / ".cookie-import.json").exists())

    def test_import_error_surfaces_worker_message(self) -> None:
        class Failing(FakeBrowserManager):
            def _cookie_import_command(self, safe_id, cookie_file):  # type: ignore[no-untyped-def]
                return [sys.executable, "-c", 'import sys; print("[ERROR] Cookie import failed: boom", file=sys.stderr); sys.exit(1)']

        with self.assertRaises(RuntimeError) as ctx:
            Failing(store=self.store).import_cookies(self.pid, [{"name": "a", "value": "b", "domain": ".x.io", "path": "/"}])
        self.assertIn("boom", str(ctx.exception))

    def test_clear_cookies_and_refuses_while_running(self) -> None:
        ff = self.store.firefox_data_dir(self.pid)
        for suffix in ("", "-wal"):
            (ff / f"cookies.sqlite{suffix}").write_text("x")
        mgr = FakeBrowserManager(store=self.store)
        mgr.clear_cookies(self.pid)
        self.assertFalse(any(ff.glob("cookies.sqlite*")))
        mgr.launch(self.pid)
        try:
            with self.assertRaises(ValueError):
                mgr.clear_cookies(self.pid)
        finally:
            mgr.stop_all(timeout=1.0)


@pytest.mark.live_browser
class TestCookieRoundTripLive(unittest.TestCase):
    def test_imported_cookie_is_sent_and_exported(self) -> None:
        seen: list = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                seen.append(self.headers.get("Cookie", ""))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        tmp = tempfile.TemporaryDirectory()
        os.environ["CAMOUDESK_HOME"] = str(Path(tmp.name) / "app")
        os.environ["CAMOUDESK_LEGACY_HOME"] = str(Path(tmp.name) / "legacy")
        try:
            store = ProfileStore()
            prof = store.create_profile({"name": "Cookie import"})
            mgr = ProcessManager(store=store)
            text = json.dumps([{"domain": "127.0.0.1", "hostOnly": True, "name": "imported", "value": "yes", "path": "/",
                                "secure": False, "httpOnly": True, "sameSite": "lax", "expirationDate": FUTURE}])
            res = mgr.import_cookies(prof.id, C.parse_cookies(text))
            self.assertGreaterEqual(res["after"], 1)

            exported = {c["name"]: c for c in C.read_profile_cookies(store.firefox_data_dir(prof.id))}
            self.assertEqual(exported["imported"]["value"], "yes")
            self.assertAlmostEqual(exported["imported"]["expires"], FUTURE, delta=5)  # unit handled correctly

            mgr.launch(prof.id, headless=True, mode=MODE_DIRECT, url_override=f"http://127.0.0.1:{port}/check")
            deadline = time.monotonic() + 60
            while not seen and time.monotonic() < deadline:
                time.sleep(0.1)
            mgr.stop(prof.id)
            self.assertTrue(any("imported=yes" in c for c in seen), seen)
        finally:
            os.environ.pop("CAMOUDESK_HOME", None)
            os.environ.pop("CAMOUDESK_LEGACY_HOME", None)
            server.shutdown()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
