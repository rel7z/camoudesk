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
        res = self.api.profiles_update(pid, {"name": "x"})
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

    def test_rows_report_effective_network(self) -> None:
        row = self.api.profiles_create({"name": "Geo", "proxy_mode": "custom",
                                        "proxy_parts": {"type": "http", "host": "10.0.0.1", "port": "8080"},
                                        "proxy_check": {"ok": True, "ip": "203.0.113.1", "timezone": "Asia/Tokyo", "country_code": "JP"}})["data"]
        self.assertEqual((row["effective_timezone"], row["effective_locale"], row["proxy_country"]), ("Asia/Tokyo", "ja-JP", "JP"))


if __name__ == "__main__":
    unittest.main()
