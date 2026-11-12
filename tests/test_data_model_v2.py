"""Tests for data model v2: saved proxies, settings, network resolution, parsers, start URLs."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camoudesk.core.geo import locale_for_country
from camoudesk.core.jsonstore import CorruptFileError
from camoudesk.core.launcher import resolve_network
from camoudesk.core.proxy_parse import parse_proxy_line, parse_proxy_list
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import MODE_DIRECT
from camoudesk.models.profile import Profile, clean_start_urls
from test_api_and_manager import FakeBrowserManager


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        self.store = ProfileStore(app_dir=self.app, legacy_dir=self.root / "legacy")
        self.proxies = ProxyStore(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestProxyStore(TempDirCase):
    def test_crud_and_password_never_exposed(self) -> None:
        px = self.proxies.create({"name": "US 1", "type": "socks5", "host": "10.0.0.1", "port": 1080,
                                  "username": "u", "password": "s3cret"})
        public = px.to_public_dict()
        self.assertEqual(public["password"], "")
        self.assertTrue(public["has_password"])
        self.assertNotIn("s3cret", json.dumps(public))
        self.assertEqual(oct(os.stat(self.proxies.path).st_mode & 0o777), "0o600")

        # An empty password from the UI keeps the stored one.
        upd = self.proxies.update(px.id, {"name": "US one", "password": ""})
        self.assertEqual((upd.name, upd.password), ("US one", "s3cret"))
        self.proxies.record_check(px.id, {"ok": True, "ip": "203.0.113.9", "timezone": "America/Chicago", "country_code": "US"})
        self.assertEqual(self.proxies.get(px.id).last_check.timezone, "America/Chicago")
        # Changing the endpoint invalidates the old check.
        moved = self.proxies.update(px.id, {"host": "10.0.0.2"})
        self.assertFalse(moved.last_check.ok)
        self.assertTrue(self.proxies.delete(px.id))
        self.assertIsNone(self.proxies.get(px.id))

    def test_validation(self) -> None:
        for bad in ({"type": "ftp", "host": "h", "port": 1}, {"type": "http", "host": "", "port": 80},
                    {"type": "http", "host": "h", "port": 70000},
                    {"type": "socks4", "host": "h", "port": 1, "username": "u"},
                    {"type": "http", "host": "h", "port": 1, "change_ip_url": "javascript:x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.proxies.create(bad)

    def test_import_skips_duplicates_and_reports_errors(self) -> None:
        text = "1.1.1.1:8080:user:pw\nuser:pw@1.1.1.1:8080\nnope\nsocks5://2.2.2.2:1080\n# comment\n"
        created, errors = self.proxies.import_text(text)
        self.assertEqual([(p.type, p.host) for p in created], [("http", "1.1.1.1"), ("socks5", "2.2.2.2")])
        self.assertEqual([e.line_no for e in errors], [3])
        again, _ = self.proxies.import_text("1.1.1.1:8080:user:pw")
        self.assertEqual(again, [])

    def test_corrupt_file_is_never_overwritten(self) -> None:
        self.proxies.path.parent.mkdir(parents=True, exist_ok=True)
        self.proxies.path.write_text("[{broken", encoding="utf-8")
        with self.assertRaises(CorruptFileError):
            self.proxies.create({"type": "http", "host": "h", "port": 1})
        self.assertEqual(self.proxies.path.read_text(encoding="utf-8"), "[{broken")


class TestSettingsStore(TempDirCase):
    def test_defaults_merge_and_validation(self) -> None:
        s = SettingsStore(self.app)
        self.assertEqual(s.get()["new_profile"]["timezone"], "auto")
        merged = s.update({"new_profile": {"os": "windows", "start_urls": ["example.com"]}, "bogus": 1})
        self.assertEqual(merged["new_profile"]["os"], "windows")
        self.assertEqual(merged["new_profile"]["start_urls"], ["https://example.com"])
        self.assertNotIn("bogus", merged)
        with self.assertRaises(ValueError):
            s.update({"new_profile": {"os": "beos"}})
        with self.assertRaises(ValueError):
            s.update({"new_profile": {"start_urls": ["javascript:alert(1)"]}})

    def test_enabling_api_generates_token(self) -> None:
        s = SettingsStore(self.app)
        on = s.update({"api": {"enabled": True}})
        self.assertGreater(len(on["api"]["token"]), 20)
        self.assertNotEqual(s.rotate_api_token(), on["api"]["token"])


class TestProfileV2(TempDirCase):
    def test_v1_entries_upgrade_and_round_trip(self) -> None:
        v1 = [{"id": "old_1", "name": "Old", "os": "windows", "proxy": "", "timezone": "Europe/London",
               "locale": "en-GB", "startup_url": "https://old.example", "block_webrtc": True}]
        self.store.registry_path.write_text(json.dumps(v1), encoding="utf-8")
        prof = self.store.get_profile("old_1")
        self.assertEqual(prof.start_urls, ["https://old.example"])
        self.store.update_profile("old_1", {"start_urls": ["https://a.io", "b.io"], "folder": "Shops", "tags": ["vip"]})
        saved = json.loads(self.store.registry_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["start_urls"], ["https://a.io", "https://b.io"])
        self.assertEqual(saved["startup_url"], "https://a.io")  # still readable by v1 code
        self.assertEqual((saved["folder"], saved["tags"], saved["timezone"]), ("Shops", ["vip"], "Europe/London"))

    def test_inline_check_cache_cleared_when_proxy_changes(self) -> None:
        prof = self.store.create_profile({"name": "P", "proxy": "http://10.0.0.1:8080",
                                          "proxy_check": {"ip": "203.0.113.1", "timezone": "Asia/Tokyo", "country_code": "jp"}})
        self.assertEqual((prof.proxy_timezone, prof.proxy_country), ("Asia/Tokyo", "JP"))
        changed = self.store.update_profile(prof.id, {"proxy": "http://10.0.0.2:8080"})
        self.assertEqual((changed.proxy_exit_ip, changed.proxy_timezone, changed.proxy_country), ("", "", ""))


class TestResolveNetwork(TempDirCase):
    def test_no_proxy_auto_means_host_settings(self) -> None:
        net = resolve_network(Profile(id="a", name="a"), self.proxies)
        self.assertEqual((net.source, net.timezone, net.locale, net.needs_check), ("none", "", "", False))

    def test_inline_auto_uses_cached_check_and_explicit_wins(self) -> None:
        p = Profile(id="a", name="a", proxy="http://10.0.0.1:8080", proxy_exit_ip="203.0.113.1",
                    proxy_timezone="Europe/Berlin", proxy_country="DE")
        net = resolve_network(p, self.proxies)
        self.assertEqual((net.source, net.timezone, net.locale, net.exit_ip), ("inline", "Europe/Berlin", "de-DE", "203.0.113.1"))
        p.timezone, p.locale = "America/Denver", "en-US"
        net = resolve_network(p, self.proxies)
        self.assertEqual((net.timezone, net.locale, net.needs_check), ("America/Denver", "en-US", False))

    def test_unchecked_proxy_needs_check(self) -> None:
        net = resolve_network(Profile(id="a", name="a", proxy="http://10.0.0.1:8080"), self.proxies)
        self.assertTrue(net.needs_check)
        self.assertEqual(net.timezone, "")

    def test_saved_proxy_and_missing_saved_proxy(self) -> None:
        px = self.proxies.create({"type": "socks5", "host": "10.0.0.9", "port": 1080, "username": "u", "password": "p"})
        self.proxies.record_check(px.id, {"ok": True, "ip": "198.51.100.4", "timezone": "Asia/Jakarta", "country_code": "ID"})
        p = Profile(id="a", name="a", proxy="http://ignored:1", proxy_id=px.id)
        net = resolve_network(p, self.proxies)
        self.assertEqual((net.source, net.proxy.host, net.timezone, net.locale), ("saved", "10.0.0.9", "Asia/Jakarta", "id-ID"))
        self.assertTrue(net.proxy.has_credentials)

        self.proxies.delete(px.id)
        gone = resolve_network(p, self.proxies)
        self.assertTrue(gone.missing_saved_proxy)
        self.assertFalse(gone.proxy.is_configured)  # never silently falls back to the inline/direct route


class TestLaunchNetwork(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = FakeBrowserManager(store=self.store, proxies=self.proxies)

    def tearDown(self) -> None:
        self.manager.stop_all(timeout=1.0)
        super().tearDown()

    def test_first_launch_checks_proxy_and_caches_location(self) -> None:
        prof = self.store.create_profile({"name": "Auto", "proxy": "http://10.0.0.1:8080"})
        fake = {"ok": True, "ip": "203.0.113.5", "timezone": "Europe/Paris", "country_code": "FR"}
        with patch("camoudesk.manager.process_manager.check_proxy_config", return_value=fake) as chk:
            self.assertTrue(self.manager.launch(prof.id)["ok"])
            net = self.manager.networks[-1]
            self.assertEqual((net.timezone, net.locale, net.exit_ip), ("Europe/Paris", "fr-FR", "203.0.113.5"))
            self.manager.stop(prof.id, timeout=1.0)
            self.manager.launch(prof.id)
            self.assertEqual(chk.call_count, 1)  # cached after the first launch
        self.assertEqual(self.store.get_profile(prof.id).proxy_timezone, "Europe/Paris")

    def test_failed_check_still_launches_with_warning(self) -> None:
        prof = self.store.create_profile({"name": "Dead", "proxy": "http://10.0.0.1:8080"})
        with patch("camoudesk.manager.process_manager.check_proxy_config", return_value={"ok": False, "error": "timed out"}):
            self.assertTrue(self.manager.launch(prof.id)["ok"])
        self.assertIn("Proxy check failed", self.manager.get_status(prof.id)["warning"])

    def test_saved_proxy_with_credentials_goes_through_forwarder(self) -> None:
        px = self.proxies.create({"type": "http", "host": "127.0.0.1", "port": 9, "username": "u", "password": "p"})
        self.proxies.record_check(px.id, {"ok": True, "ip": "203.0.113.7", "timezone": "UTC", "country_code": "GB"})
        prof = self.store.create_profile({"name": "Saved", "proxy_id": px.id})
        res = self.manager.launch(prof.id)
        self.assertEqual(res["mode"], MODE_DIRECT)
        local = self.manager.local_proxies[-1]
        self.assertIsNotNone(local)
        self.assertEqual(local.host, "127.0.0.1")
        self.assertFalse(local.has_credentials)

    def test_missing_saved_proxy_refuses_to_launch(self) -> None:
        prof = self.store.create_profile({"name": "Orphan", "proxy_id": "px_deleted_1"})
        res = self.manager.launch(prof.id)
        self.assertFalse(res["ok"])
        self.assertIn("saved proxy was deleted", res["error"])


class TestParsersAndHelpers(unittest.TestCase):
    def test_proxy_line_formats(self) -> None:
        cases = {
            "1.2.3.4:8080": ("http", "1.2.3.4", 8080, "", ""),
            "1.2.3.4:8080:user:pa:ss": ("http", "1.2.3.4", 8080, "user", "pa%3Ass"),
            "user:p@ss@host.example:3128": ("http", "host.example", 3128, "user", "p%40ss"),
            "socks5://u:p@h:1080": ("socks5", "h", 1080, "u", "p"),
            "h:1080@u:p": ("http", "h", 1080, "u", "p"),
            "[2001:db8::1]:8080:u:p": ("http", "2001:db8::1", 8080, "u", "p"),
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                c = parse_proxy_line(line)
                self.assertEqual((c.scheme, c.host, c.port, c.username, c.password), want)
        self.assertEqual(parse_proxy_line("h:1", default_scheme="socks5").scheme, "socks5")
        _, errors = parse_proxy_list("ok.host:1\nbad\nh:x")
        self.assertEqual([e.line_no for e in errors], [2, 3])

    def test_locale_for_country_is_deterministic(self) -> None:
        self.assertEqual([locale_for_country(c) for c in ("US", "DE", "JP", "br", "XX", None)],
                         ["en-US", "de-DE", "ja-JP", "pt-BR", None, None])
        self.assertEqual({locale_for_country("CH") for _ in range(20)}, {"de-CH"})

    def test_start_url_validation(self) -> None:
        self.assertEqual(clean_start_urls(["example.com:8080/x", "about:blank", "a.io", "a.io"]),
                         ["https://example.com:8080/x", "about:blank", "https://a.io"])
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "JavaScript:x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_start_urls([bad])
        self.assertEqual(clean_start_urls(["javascript:x", "https://ok.io"], strict=False), ["https://ok.io"])


if __name__ == "__main__":
    unittest.main()
