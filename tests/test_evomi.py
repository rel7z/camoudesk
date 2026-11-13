"""Evomi integration: password options, per-profile sessions, launch resolution, targeting API."""

from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

from camoudesk.core.launcher import resolve_network
from camoudesk.core.providers import evomi
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.storage import ProfileStore
from camoudesk.models.proxy import SavedProxy

# Same shape as a real Evomi line; the password is made up.
LINE = "http://core-residential.evomi.com:1000:acct123:Base_pw-9Zq_session-A6EGX87UI_lifetime-60"


def password_of(cfg) -> str:
    return unquote(cfg.password)


class TestPasswordOptions(unittest.TestCase):
    def test_split_peels_known_options_only(self) -> None:
        self.assertEqual(evomi.split_password("Base_pw-9Zq_session-A6EGX87UI_lifetime-60"),
                         ("Base_pw-9Zq", {"session": "A6EGX87UI", "lifetime": "60"}))
        self.assertEqual(evomi.split_password("p_country-US_city-new.york_hardsession-abc123")[0], "p")
        self.assertEqual(evomi.split_password("plain_pass-word"), ("plain_pass-word", {}))

    def test_compose_order_and_modes(self) -> None:
        opts = evomi.clean_opts({"country": "us", "city": "New York", "mode": "sticky", "lifetime": 90})
        self.assertEqual(evomi.compose_password("b", opts, "sess123456"),
                         "b_country-US_city-new.york_session-sess123456_lifetime-90")
        self.assertEqual(evomi.compose_password("b", {**opts, "mode": "hard"}, "s1s2s3"),
                         "b_country-US_city-new.york_hardsession-s1s2s3")
        self.assertEqual(evomi.compose_password("b", {**opts, "mode": "locked"}, "s1s2s3"),
                         "b_country-US_city-new.york_lockedsession-s1s2s3")
        self.assertEqual(evomi.compose_password("b", {**opts, "mode": "rotating"}, "s1s2s3"), "b_country-US_city-new.york")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            evomi.clean_opts({"lifetime": 1441})
        self.assertEqual(evomi.clean_opts({})["lifetime"], evomi.DEFAULT_LIFETIME)
        self.assertEqual(evomi.clean_opts({"country": "USA", "mode": "weird"}, partial=True), {})

    def test_session_ids_are_stable_alphanumeric_and_rotate(self) -> None:
        a = evomi.session_id("prof_1")
        self.assertRegex(a, r"^[a-z0-9]{10}$")
        self.assertEqual(a, evomi.session_id("prof_1", 0))
        self.assertNotEqual(a, evomi.session_id("prof_1", 1))
        self.assertNotEqual(a, evomi.session_id("prof_2"))

    def test_detect(self) -> None:
        self.assertEqual(evomi.detect("core-residential.evomi.com"), "rpc")
        self.assertEqual(evomi.detect("rp.evomi.com"), "rp")
        self.assertIsNone(evomi.detect("evomi.com.attacker.net"))
        self.assertIsNone(evomi.detect("notevomi.com"))


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.proxies = ProxyStore(self.store.app_dir)
        created, errors = self.proxies.import_text(LINE)
        self.assertEqual(errors, [])
        self.px = created[0]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def profile(self, name: str, **extra):
        return self.store.create_profile({"name": name, "proxy_id": self.px.id, **extra})


class TestSavedEvomiProxy(StoreCase):
    def test_import_strips_session_and_keeps_lifetime(self) -> None:
        self.assertEqual(self.px.password, "Base_pw-9Zq")
        self.assertEqual(self.px.provider["mode"], "sticky")
        self.assertEqual(self.px.provider["lifetime"], 60)
        self.assertEqual(self.px.provider["product"], "rpc")
        public = self.px.to_public_dict()
        self.assertEqual(public["password"], "")
        self.assertIn("Core Residential", public["provider_summary"])
        self.assertNotIn("Base_pw", str(public))

    def test_same_account_other_country_is_not_a_duplicate(self) -> None:
        created, _ = self.proxies.import_text(LINE.replace("_session", "_country-DE_session"))
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].provider["country"], "DE")
        again, _ = self.proxies.import_text(LINE)
        self.assertEqual(again, [])

    def test_form_update_sets_targeting_and_resets_check(self) -> None:
        self.proxies.record_check(self.px.id, {"ok": True, "ip": "203.0.113.1", "country_code": "US"})
        updated = self.proxies.update(self.px.id, {"provider": {"country": "gb", "mode": "hard"}})
        self.assertEqual((updated.provider["country"], updated.provider["mode"]), ("GB", "hard"))
        self.assertEqual(updated.password, "Base_pw-9Zq")  # blank password field keeps the base
        self.assertFalse(updated.last_check.ok)


class TestPerProfileSessions(StoreCase):
    def test_profiles_sharing_one_account_get_their_own_session(self) -> None:
        a, b = self.profile("A"), self.profile("B")
        pa = password_of(resolve_network(a, self.proxies).proxy)
        pb = password_of(resolve_network(b, self.proxies).proxy)
        self.assertNotEqual(pa, pb)
        self.assertEqual(pa, f"Base_pw-9Zq_session-{evomi.session_id(a.id)}_lifetime-60")
        # Every launch path resolves the same credentials (resolve_network is pure).
        self.assertEqual(pa, password_of(resolve_network(self.store.get_profile(a.id), self.proxies).proxy))

    def test_profile_overrides_and_new_ip(self) -> None:
        a = self.profile("A", provider_opts={"country": "US", "city": "Los Angeles", "mode": "hard", "rotation": 7})
        self.assertNotIn("rotation", a.provider_opts)  # not settable from outside
        first = password_of(resolve_network(a, self.proxies).proxy)
        self.assertTrue(first.startswith("Base_pw-9Zq_country-US_city-los.angeles_hardsession-"))
        self.store.new_provider_session([a.id])
        second = password_of(resolve_network(self.store.get_profile(a.id), self.proxies).proxy)
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(evomi.session_id(a.id, 1)))

    def test_location_is_cached_per_session_and_always_rechecked(self) -> None:
        a = self.profile("A")
        net = resolve_network(a, self.proxies)
        self.assertTrue(net.needs_check and net.session_key and not net.timezone)
        self.store.record_session_check(a.id, {"ok": True, "ip": "198.51.100.7", "timezone": "America/Denver",
                                               "country_code": "US"}, net.session_key)
        net = resolve_network(self.store.get_profile(a.id), self.proxies)
        self.assertEqual((net.timezone, net.locale, net.exit_ip), ("America/Denver", "en-US", "198.51.100.7"))
        self.assertTrue(net.needs_check)  # sessions can change IP: verify at every launch
        # A new IP, or another country, invalidates the cached location.
        self.store.update_profile(a.id, {"provider_opts": {"country": "DE"}})
        self.assertEqual(resolve_network(self.store.get_profile(a.id), self.proxies).timezone, "")

    def test_saved_proxy_check_never_feeds_profile_timezone(self) -> None:
        a = self.profile("A")
        self.proxies.record_check(self.px.id, {"ok": True, "ip": "203.0.113.5", "timezone": "Asia/Tokyo", "country_code": "JP"})
        self.assertEqual(resolve_network(a, self.proxies).timezone, "")

    def test_duplicate_gets_its_own_session_and_rotating_flag(self) -> None:
        a = self.profile("A", provider_opts={"country": "FR"})
        dup = self.store.clone_profile(a.id, copy_data=True)
        self.assertEqual(dup.provider_opts.get("country"), "FR")
        self.assertNotEqual(resolve_network(dup, self.proxies).session_key, resolve_network(a, self.proxies).session_key)
        r = self.profile("R", provider_opts={"mode": "rotating", "country": "FR"})
        net = resolve_network(r, self.proxies)
        self.assertTrue(net.rotating)
        self.assertEqual(password_of(net.proxy), "Base_pw-9Zq_country-FR")  # no session: new IP per request

    def test_credentials_stay_out_of_diagnostics(self) -> None:
        from camoudesk.manager.process_manager import ProcessManager

        a = self.profile("A")
        text = ProcessManager(store=self.store, adopt_running=False).diagnostics(a.id)
        self.assertNotIn("Base_pw", text)
        self.assertNotIn(evomi.session_id(a.id), text)


class TestTargetsApi(unittest.TestCase):
    def response(self, status: int, body=None):
        resp = MagicMock(status_code=status)
        resp.json.return_value = body
        return resp

    def test_parses_settings_block_for_product(self) -> None:
        body = {"success": True, "data": {
            "rpc": {"countries": {"US": "United States", "DE": "Germany"},
                    "cities": [{"country": "US", "name": "New York"}, {"country": "US", "name": "Los Angeles"}],
                    "regions": {"US": ["california"]}},
            "rp": {"countries": ["GB"]}}}
        with patch("requests.get", return_value=self.response(200, body)) as get:
            targets = evomi.fetch_targets("k3y", "rpc")
        self.assertEqual(get.call_args.kwargs["headers"]["x-apikey"], "k3y")
        self.assertEqual(targets["countries"], ["DE", "US"])
        self.assertEqual(targets["cities"]["US"], ["los.angeles", "new.york"])
        self.assertEqual(targets["regions"]["US"], ["california"])

    def test_errors_are_readable(self) -> None:
        for status, text in ((401, "rejected"), (429, "rate limit"), (500, "HTTP 500")):
            with patch("requests.get", return_value=self.response(status)):
                with self.assertRaisesRegex(evomi.EvomiApiError, text):
                    evomi.fetch_targets("k", "rpc")


class TestSavedProxyModel(unittest.TestCase):
    def test_non_evomi_proxies_are_untouched(self) -> None:
        px = SavedProxy.from_dict({"id": "px_1", "type": "http", "host": "proxy.example.com", "port": 8000,
                                   "username": "u", "password": "p_session-abc123"})
        self.assertEqual(px.provider, {})
        self.assertEqual(px.password, "p_session-abc123")
        self.assertTrue(re.search("p_session-abc123", password_of(px.to_config())))


if __name__ == "__main__":
    unittest.main()


class TestBridge(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        from camoudesk.manager.process_manager import ProcessManager
        from camoudesk.ui.api import DesktopBridgeApi

        self.api = DesktopBridgeApi(store=self.store, manager=ProcessManager(store=self.store, adopt_running=False),
                                    proxies=self.proxies)

    def test_row_shows_provider_session_without_secrets(self) -> None:
        a = self.profile("A", provider_opts={"country": "US"})
        row = self.api.profiles_get(a.id)["data"]
        self.assertEqual(row["provider"]["session"], evomi.session_id(a.id))
        self.assertIn("US", row["provider"]["summary"])
        self.assertNotIn("Base_pw", str(row))

    def test_check_uses_profile_session_and_caches_it(self) -> None:
        a = self.profile("A")
        seen = []

        def fake_check(cfg, **_kw):
            seen.append(password_of(cfg))
            return {"ok": True, "ip": "198.51.100.9", "timezone": "Europe/Berlin", "country_code": "DE"}

        with patch("camoudesk.ui.api.check_proxy_config", side_effect=fake_check):
            res = self.api.proxy_test({"proxy_id": self.px.id, "profile_id": a.id})["data"]
            self.assertEqual(res["session"], evomi.session_id(a.id))
            self.assertTrue(seen[-1].endswith(f"session-{evomi.session_id(a.id)}_lifetime-60"))
            self.assertEqual(resolve_network(self.store.get_profile(a.id), self.proxies).timezone, "Europe/Berlin")
            # Unsaved form targeting is checked as-is but not cached as the profile's location.
            self.api.proxy_test({"proxy_id": self.px.id, "profile_id": a.id, "provider_opts": {"country": "JP"}})
            self.assertIn("_country-JP_", seen[-1])
            self.assertEqual(resolve_network(self.store.get_profile(a.id), self.proxies).timezone, "Europe/Berlin")

    def test_new_ip_skips_profiles_without_provider(self) -> None:
        a = self.profile("A")
        plain = self.store.create_profile({"name": "Direct"})
        res = self.api.profiles_new_ip([a.id, plain.id])["data"]
        self.assertEqual((res["changed"], res["skipped_no_provider"]), (1, 1))
        self.assertEqual(self.store.get_profile(a.id).provider_opts["rotation"], 1)

    def test_api_key_is_validated_stored_0600_and_never_returned(self) -> None:
        import os
        import stat

        with patch("camoudesk.core.providers.evomi.fetch_targets", side_effect=evomi.EvomiApiError("Evomi rejected the API key")):
            self.assertFalse(self.api.providers_set_key("evomi", "bad-key")["ok"])
        self.assertFalse(self.api.providers_status()["data"]["evomi"]["has_key"])
        targets = {"countries": ["US"], "regions": {}, "cities": {"US": ["new.york"]}, "fetched_at": "x"}
        with patch("camoudesk.core.providers.evomi.fetch_targets", return_value=targets):
            res = self.api.providers_set_key("evomi", "s3cret-key")
        self.assertTrue(res["ok"])
        self.assertNotIn("s3cret-key", str(res) + str(self.api.providers_status()) + str(self.api.settings_get()))
        mode = stat.S_IMODE(os.stat(self.store.app_dir / "providers.json").st_mode)
        self.assertEqual(mode, 0o600)
        with patch("camoudesk.core.providers.evomi.fetch_targets") as fetch:
            got = self.api.providers_targets("evomi", "rpc")["data"]
            fetch.assert_not_called()  # served from the day's cache
        self.assertEqual((got["source"], got["cities"]["US"]), ("evomi", ["new.york"]))
        self.api.providers_clear_key("evomi")
        builtin = self.api.providers_targets("evomi", "rpc")["data"]
        self.assertEqual(builtin["source"], "builtin")
        self.assertIn("US", builtin["countries"])


class TestSdkLocation(StoreCase):
    def test_every_launch_path_checks_the_session_location_first(self) -> None:
        """The SDK used to skip the pre-launch check, so "auto" fell back to the host timezone."""
        from camoudesk.core.launcher import ensure_location

        a = self.profile("A")
        net = resolve_network(a, self.proxies)
        fake = {"ok": True, "ip": "91.40.180.39", "timezone": "Europe/Berlin", "country_code": "DE"}
        with patch("camoudesk.core.proxy.check_proxy_config", return_value=fake) as chk:
            out = ensure_location(a, net, self.store, self.proxies)
        self.assertEqual(password_of(chk.call_args.args[0]), password_of(net.proxy))
        self.assertEqual((out.timezone, out.locale), ("Europe/Berlin", "de-DE"))
        self.assertEqual(self.store.get_profile(a.id).proxy_timezone, "Europe/Berlin")
        self.assertEqual(self.proxies.get(self.px.id).last_check.ok, False)  # not the shared proxy
        import inspect

        from camoudesk import sdk

        self.assertIn("ensure_location(", inspect.getsource(sdk.open_profile))
