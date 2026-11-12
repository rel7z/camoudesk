"""Tests for the JS bridge (ui/api.py): envelopes, secrets, proxy modes, defaults."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.ui.api import DesktopBridgeApi
from test_api_and_manager import FakeBrowserManager


class TestBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.proxies = ProxyStore(self.store.app_dir)
        self.settings = SettingsStore(self.store.app_dir)
        self.manager = FakeBrowserManager(store=self.store, proxies=self.proxies)
        self.api = DesktopBridgeApi(self.store, self.manager, self.proxies, self.settings)

    def tearDown(self) -> None:
        self.manager.stop_all(timeout=1.0)
        self.tmp.cleanup()

    def test_errors_use_the_envelope(self) -> None:
        self.assertEqual(self.api.profiles_get("nope_1"), {"ok": False, "error": "Profile not found"})
        res = self.api.profiles_create({"name": ""})
        self.assertFalse(res["ok"])
        self.assertIn("name", res["error"].lower())

    def test_create_applies_settings_defaults(self) -> None:
        self.settings.update({"new_profile": {"os": "windows", "block_webrtc": False, "start_urls": ["https://start.example"]}})
        row = self.api.profiles_create({"name": "From defaults"})["data"]
        self.assertEqual((row["os"], row["block_webrtc"], row["start_urls"]), ("windows", False, ["https://start.example"]))
        row = self.api.profiles_create({"name": "Override", "os": "linux"})["data"]
        self.assertEqual(row["os"], "linux")

    def test_custom_proxy_password_never_returned_and_kept_on_blank(self) -> None:
        row = self.api.profiles_create({"name": "C", "proxy_mode": "custom",
                                        "proxy_parts": {"type": "socks5", "host": "10.0.0.1", "port": "1080", "username": "me", "password": "pw!@:"}})["data"]
        self.assertNotIn("pw!@:", json.dumps(row))
        self.assertNotIn("proxy", row)  # raw URL with credentials is stripped
        self.assertEqual(row["proxy_parts"], {"type": "socks5", "host": "10.0.0.1", "port": 1080, "username": "me", "has_password": True})

        # Saving the form again with a blank password keeps the stored one.
        self.api.profiles_update(row["id"], {"proxy_mode": "custom",
                                             "proxy_parts": {"type": "socks5", "host": "10.0.0.1", "port": "1080", "username": "me", "password": ""}})
        stored = self.store.get_profile(row["id"]).proxy
        self.assertIn("pw%21%40%3A@", stored)
        # A different host never inherits the old password.
        self.api.profiles_update(row["id"], {"proxy_mode": "custom",
                                             "proxy_parts": {"type": "socks5", "host": "10.0.0.2", "port": "1080", "username": "me", "password": ""}})
        self.assertNotIn("pw", self.store.get_profile(row["id"]).proxy)

    def test_saved_and_none_modes(self) -> None:
        px = self.proxies.create({"name": "Saved", "type": "http", "host": "10.0.0.5", "port": 3128})
        row = self.api.profiles_create({"name": "S", "proxy_mode": "saved", "proxy_id": px.id})["data"]
        self.assertEqual((row["proxy_mode"], row["proxy_label"], row["proxy_id"]), ("saved", "Saved", px.id))
        bad = self.api.profiles_update(row["id"], {"proxy_mode": "saved", "proxy_id": "px_missing_1"})
        self.assertFalse(bad["ok"])
        row = self.api.profiles_update(row["id"], {"proxy_mode": "none"})["data"]
        self.assertEqual((row["proxy_mode"], row["proxy_label"], row["proxy_id"]), ("none", "Direct", ""))

    def test_update_blocked_while_running(self) -> None:
        pid = self.store.list_profiles()[0].id
        self.manager.launch(pid)
        res = self.api.profiles_update(pid, {"name": "x", "os": "linux"})
        self.assertFalse(res["ok"])
        self.assertIn("Stop the running browser", res["error"])

    def test_proxy_parse_and_test(self) -> None:
        parts = self.api.proxy_parse("203.0.113.9:8000:alice:s3cret", "http")["data"]
        self.assertEqual((parts["host"], parts["port"], parts["username"], parts["password"]), ("203.0.113.9", 8000, "alice", "s3cret"))
        self.assertFalse(self.api.proxy_parse("not a proxy")["ok"])

        fake = {"ok": True, "ip": "203.0.113.9", "country_code": "BR", "timezone": "America/Sao_Paulo"}
        with patch("camoudesk.ui.api.check_proxy_config", return_value=dict(fake)):
            res = self.api.proxy_test({"proxy_parts": {"type": "http", "host": "203.0.113.9", "port": 8000}})["data"]
        self.assertEqual(res["locale"], "pt-BR")  # preview of what "auto" locale will use

        px = self.proxies.create({"type": "http", "host": "10.0.0.7", "port": 80})
        with patch("camoudesk.ui.api.check_proxy_config", return_value=dict(fake)):
            self.api.proxy_test({"proxy_id": px.id})
        self.assertEqual(self.proxies.get(px.id).last_check.country_code, "BR")  # recorded on the saved proxy

    def test_proxy_manager_crud_never_exposes_password(self) -> None:
        row = self.api.proxies_create({"name": "P1", "type": "socks5", "host": "10.0.0.9", "port": 1080, "username": "u", "password": "topsecret"})["data"]
        self.assertNotIn("topsecret", json.dumps(row))
        self.assertTrue(row["has_password"])
        self.assertEqual((row["used_by"], row["checking"]), (0, False))
        listed = self.api.proxies_list()["data"]
        self.assertNotIn("topsecret", json.dumps(listed))
        upd = self.api.proxies_update(row["id"], {"name": "P1b", "password": ""})["data"]
        self.assertEqual(upd["name"], "P1b")
        self.assertEqual(self.proxies.get(row["id"]).password, "topsecret")

    def test_deleting_in_use_proxy_needs_force_and_never_falls_back_to_direct(self) -> None:
        px = self.api.proxies_create({"type": "http", "host": "10.0.0.9", "port": 8080})["data"]
        prof = self.api.profiles_create({"name": "User", "proxy_mode": "saved", "proxy_id": px["id"]})["data"]
        first = self.api.proxies_delete(px["id"])["data"]
        self.assertEqual(first, {"deleted": False, "in_use": 1})
        self.assertTrue(self.api.proxies_delete(px["id"], True)["data"]["deleted"])
        row = self.api.profiles_get(prof["id"])["data"]
        self.assertTrue(row["proxy_missing"])
        launch = self.api.profiles_launch(prof["id"])
        self.assertFalse(launch["ok"])
        self.assertIn("saved proxy was deleted", launch["error"])

    def test_check_all_runs_in_background_and_notifies(self) -> None:
        import threading
        ids = [self.api.proxies_create({"type": "http", "host": f"10.0.0.{i}", "port": 80})["data"]["id"] for i in range(3)]
        notified = threading.Event()
        calls = []
        self.api.set_proxy_notifier(lambda: (calls.append(1), notified.set()))
        fake = {"ok": True, "ip": "203.0.113.4", "country_code": "CA", "timezone": "America/Toronto", "latency_ms": 50}
        with patch("camoudesk.ui.api.check_proxy_config", return_value=dict(fake)):
            started = self.api.proxies_check_all()["data"]
            self.assertEqual(started["started"], 3)
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline and not all(self.proxies.get(i).last_check.ok for i in ids):
                __import__("time").sleep(0.05)
        self.assertTrue(all(self.proxies.get(i).last_check.country_code == "CA" for i in ids))
        self.assertGreaterEqual(len(calls), 6)  # checking -> done for each proxy

    def test_import_preview_and_import(self) -> None:
        prev = self.api.proxies_import_preview("1.1.1.1:80:u:p\nnope\nsocks5://2.2.2.2:1080", "http")["data"]
        self.assertEqual(len(prev["proxies"]), 2)
        self.assertNotIn(":p@", json.dumps(prev))  # previews are masked
        self.assertEqual(prev["errors"][0]["line"], 2)
        res = self.api.proxies_import("1.1.1.1:80:u:p\n3.3.3.3:8080", "http", "US-", False)["data"]
        self.assertEqual(res["created"], 2)
        self.assertEqual(sorted(p.name for p in self.proxies.list()), ["US-1.1.1.1:80", "US-3.3.3.3:8080"])

    def test_rotate_calls_change_url_directly_then_rechecks(self) -> None:
        px = self.api.proxies_create({"type": "http", "host": "10.0.0.9", "port": 80, "change_ip_url": "https://rotate.example/x"})["data"]
        no_url = self.api.proxies_create({"type": "http", "host": "10.0.0.8", "port": 80})["data"]
        self.assertFalse(self.api.proxies_rotate(no_url["id"])["ok"])
        resp = type("R", (), {"status_code": 200})()
        fake = {"ok": True, "ip": "203.0.113.200", "country_code": "US", "timezone": "UTC"}
        with patch("requests.get", return_value=resp) as get, patch("camoudesk.ui.api.check_proxy_config", return_value=dict(fake)), \
                patch("camoudesk.ui.api.time.sleep"):
            row = self.api.proxies_rotate(px["id"])["data"]
        self.assertEqual(get.call_args.args[0], "https://rotate.example/x")
        self.assertNotIn("proxies", get.call_args.kwargs)  # not routed through the proxy itself
        self.assertEqual(row["result"]["ip"], "203.0.113.200")

    def test_bulk_assign_proxy_skips_running(self) -> None:
        px = self.api.proxies_create({"type": "http", "host": "10.0.0.9", "port": 80})["data"]
        a = self.api.profiles_create({"name": "A"})["data"]["id"]
        b = self.api.profiles_create({"name": "B"})["data"]["id"]
        self.manager.launch(b)
        res = self.api.profiles_assign_proxy([a, b], px["id"])["data"]
        self.assertEqual(res, {"changed": 1, "skipped_running": 1})
        self.assertEqual(self.store.get_profile(a).proxy_id, px["id"])
        self.assertEqual(self.store.get_profile(b).proxy_id, "")
        self.api.profiles_assign_proxy([a], "")
        self.assertEqual(self.store.get_profile(a).proxy_id, "")

    def test_rows_report_effective_network(self) -> None:
        row = self.api.profiles_create({"name": "Geo", "proxy_mode": "custom",
                                        "proxy_parts": {"type": "http", "host": "10.0.0.1", "port": "8080"},
                                        "proxy_check": {"ok": True, "ip": "203.0.113.1", "timezone": "Asia/Tokyo", "country_code": "JP"}})["data"]
        self.assertEqual((row["effective_timezone"], row["effective_locale"], row["proxy_country"]), ("Asia/Tokyo", "ja-JP", "JP"))


if __name__ == "__main__":
    unittest.main()
