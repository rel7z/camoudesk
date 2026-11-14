"""Anonymity check: grading (core/identity_check.py), the bridge, and a live probe run."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from camoudesk.core import identity_check
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore

UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"


def raw_result(**over):  # type: ignore[no-untyped-def]
    base = {
        "navigator": {"userAgent": UA_WIN, "platform": "Win32", "oscpu": "Windows NT 10.0; Win64; x64", "language": "zh-HK",
                      "languages": ["zh-HK"], "hardwareConcurrency": 8, "webdriver": False},
        "screen": {"width": 1920, "height": 1080, "devicePixelRatio": 1},
        "intl": {"timeZone": "Asia/Hong_Kong"},
        "webgl": {"available": True, "renderer": "ANGLE (AMD, Radeon), or similar"},
        "canvas": {"stable": False}, "audio": {"stable": True},
        "fonts": {"Segoe UI": True, "Helvetica Neue": False, "Apple Color Emoji": False},
        "native": {"notNative": [], "ownNavigatorProps": []},
        "worker": {"userAgent": UA_WIN, "platform": "Win32", "hardwareConcurrency": 8, "timeZone": "Asia/Hong_Kong"},
        "iframe": {"userAgent": UA_WIN, "platform": "Win32", "hardwareConcurrency": 8, "screenWidth": 1920},
        "ip": {"ip": "58.177.7.62", "country": "HK", "city": "Hong Kong", "timezone": "Asia/Hong_Kong"},
        "webrtc": {"available": False, "candidates": []},
        "errors": [],
    }
    for key, value in over.items():
        base[key] = {**base[key], **value} if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return base


EXPECTED = {"os": "windows", "user_agent": UA_WIN, "screen": "1920x1080", "cores": 8, "gpu": "ANGLE (AMD, Radeon), or similar",
            "canvas_real": False, "proxy": True, "real_ip": "92.119.177.22", "country": "HK", "timezone": "Asia/Hong_Kong",
            "locale": "", "block_webgl": False}


def status_of(report, key):  # type: ignore[no-untyped-def]
    return next(i["status"] for i in report["items"] if i["id"] == key)


class TestGrading(unittest.TestCase):
    def test_clean_proxy_profile_only_warns_about_a_covered_canvas(self) -> None:
        rep = identity_check.evaluate(raw_result(), EXPECTED)
        self.assertEqual(rep["status"], "warn")
        self.assertEqual([i["id"] for i in rep["items"] if i["status"] != "pass"], ["canvas"])
        self.assertEqual(rep["total"], len(identity_check.LABELS))

    def test_real_ip_leaks_fail(self) -> None:
        rep = identity_check.evaluate(raw_result(ip={"ip": "92.119.177.22"}), EXPECTED)
        self.assertEqual(status_of(rep, "ip"), "fail")
        rtc = identity_check.evaluate(raw_result(webrtc={"available": True, "candidates": ["srflx 92.119.177.22"]}), EXPECTED)
        self.assertEqual(status_of(rtc, "webrtc"), "fail")
        local = identity_check.evaluate(raw_result(webrtc={"available": True, "candidates": ["host 192.168.1.20"]}), EXPECTED)
        self.assertEqual(status_of(local, "webrtc"), "warn")
        mdns = identity_check.evaluate(raw_result(webrtc={"available": True, "candidates": ["host 3f1c.local", "srflx 58.177.7.62"]}), EXPECTED)
        self.assertEqual(status_of(mdns, "webrtc"), "pass")

    def test_timezone_and_language_follow_the_ip(self) -> None:
        tz = identity_check.evaluate(raw_result(intl={"timeZone": "Asia/Jakarta"}), {**EXPECTED, "timezone": ""})
        self.assertEqual(status_of(tz, "timezone"), "fail")
        no_proxy = identity_check.evaluate(raw_result(intl={"timeZone": "Asia/Jakarta"}), {**EXPECTED, "timezone": "", "proxy": False})
        self.assertEqual(status_of(no_proxy, "timezone"), "warn")
        self.assertEqual(status_of(no_proxy, "ip"), "warn")
        lang = identity_check.evaluate(raw_result(navigator={"language": "de-DE"}), EXPECTED)
        self.assertEqual(status_of(lang, "language"), "warn")

    def test_inconsistencies_and_patched_getters_fail(self) -> None:
        rep = identity_check.evaluate(raw_result(worker={"platform": "MacIntel"}, native={"notNative": ["Navigator.userAgent"]},
                                                 navigator={"webdriver": True}), EXPECTED)
        for key in ("consistency", "native", "webdriver"):
            self.assertEqual(status_of(rep, key), "fail", key)
        ua = identity_check.evaluate(raw_result(navigator={"platform": "MacIntel"}), EXPECTED)
        self.assertEqual(status_of(ua, "user_agent"), "fail")

    def test_user_agent_version_differences_are_not_flagged(self) -> None:
        older = {**EXPECTED, "user_agent": UA_WIN.replace("158.0", "151.0")}
        self.assertEqual(status_of(identity_check.evaluate(raw_result(), older), "user_agent"), "pass")

    def test_real_canvas_and_foreign_fonts(self) -> None:
        mac = {**EXPECTED, "canvas_real": True}
        self.assertEqual(status_of(identity_check.evaluate(raw_result(canvas={"stable": True}), mac), "canvas"), "pass")
        self.assertEqual(status_of(identity_check.evaluate(raw_result(canvas={"stable": False}), mac), "canvas"), "fail")
        fonts = identity_check.evaluate(raw_result(fonts={"Apple Color Emoji": True}), EXPECTED)
        self.assertEqual(status_of(fonts, "fonts"), "warn")

    def test_engine_claim_overrides_stored_summary(self) -> None:
        exp = identity_check.expected_from_claim({**EXPECTED, "screen": "1366x768"}, {"screen.width": 1920, "screen.height": 1080})
        self.assertEqual(exp["screen"], "1920x1080")


class TestBridge(unittest.TestCase):
    def setUp(self) -> None:
        from camoudesk.manager.process_manager import ProcessManager
        from camoudesk.ui.api import DesktopBridgeApi

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        SettingsStore(self.store.app_dir).update({"search_engine": "none"})
        self.api = DesktopBridgeApi(store=self.store, manager=ProcessManager(store=self.store, adopt_running=False))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_check_saves_a_report_and_rows_show_its_summary(self) -> None:
        prof = self.store.create_profile({"name": "W", "os": "windows"})
        with patch.object(self.api.manager, "check_identity", return_value=raw_result(ip={"ip": "203.0.113.4"})), \
                patch.object(self.api.ctx.identity_service, "real_ip", return_value="92.119.177.22"):
            rep = self.api.profiles_check_identity(prof.id)["data"]
        self.assertIn(rep["status"], ("pass", "warn", "fail"))
        saved = json.loads((self.store.profile_dir(prof.id) / identity_check.REPORT_FILENAME).read_text())
        self.assertEqual(saved["items"], rep["items"])
        self.assertEqual(self.api.profiles_identity_report(prof.id)["data"]["status"], rep["status"])
        row = self.api.profiles_get(prof.id)["data"]
        self.assertEqual(row["identity"]["passed"], rep["passed"])

    def test_running_profiles_cannot_be_checked(self) -> None:
        prof = self.store.create_profile({"name": "R", "os": "macos"})
        with patch.object(self.api.manager, "is_running", return_value=True):
            res = self.api.profiles_check_identity(prof.id)
        self.assertFalse(res["ok"])
        self.assertIn("Stop the browser", res["error"])

    def test_test_sites_open_in_tabs(self) -> None:
        prof = self.store.create_profile({"name": "T", "os": "macos"})
        with patch.object(self.api.manager, "launch", return_value={"ok": True, "pid": 1}) as launch:
            self.assertTrue(self.api.profiles_open_test_sites(prof.id)["ok"])
        self.assertEqual(launch.call_args.kwargs["url_override"], list(self.api.TEST_SITES))


@pytest.mark.live_browser
class TestProbeLive(TestBridge):
    def test_probe_reads_a_consistent_engine_level_identity(self) -> None:
        import os

        prof = self.store.create_profile({"name": "Live", "os": "windows"})
        with patch.dict(os.environ, {"CAMOUDESK_HOME": str(self.store.app_dir)}):  # the worker's data folder
            raw = self.api.manager.check_identity(prof.id)
        self.assertEqual(raw["navigator"]["userAgent"], raw["worker"]["userAgent"])
        self.assertEqual(raw["native"]["notNative"], [])
        self.assertFalse(raw["navigator"]["webdriver"])
        self.assertIn("Windows", raw["navigator"]["userAgent"])
        self.assertEqual(raw["claimed"]["navigator.userAgent"], raw["navigator"]["userAgent"])

    def test_check_saves_a_report_and_rows_show_its_summary(self) -> None:  # offline version above
        pass

    def test_running_profiles_cannot_be_checked(self) -> None:  # offline version above
        pass

    def test_test_sites_open_in_tabs(self) -> None:  # offline version above
        pass


if __name__ == "__main__":
    unittest.main()
