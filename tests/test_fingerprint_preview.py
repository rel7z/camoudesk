"""New profile "Randomize": a previewed fingerprint is exactly the one the profile gets."""

import json
from pathlib import Path
import tempfile
import time
import unittest

from camoudesk.core.fingerprint import FINGERPRINT_FILENAME, generate_verified_fingerprint, save_fingerprint
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.ui.api import DesktopBridgeApi


class PreviewCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        SettingsStore(self.store.app_dir).update({"search_engine": "none"})
        self.api = DesktopBridgeApi(store=self.store, manager=ProcessManager(store=self.store, adopt_running=False))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def preview(self, os_: str, screen=None) -> dict:
        res = self.api.fingerprint_preview(os_, screen)
        self.assertTrue(res["ok"], res)
        return res["data"]

    def saved_fp(self, profile_id: str) -> dict:
        return json.loads((self.store.profile_dir(profile_id) / FINGERPRINT_FILENAME).read_text())


class TestPreview(PreviewCase):
    def test_created_profile_uses_the_previewed_device(self) -> None:
        pv = self.preview("windows")
        row = self.api.profiles_create({"name": "P", "os": "windows", "fp_preview": pv["token"]})["data"]
        self.assertEqual(row["fingerprint_summary"], pv["summary"])
        fp = self.saved_fp(row["id"])
        self.assertEqual(fp["navigator"]["userAgent"], pv["summary"]["user_agent"])

    def test_preview_writes_nothing_and_keeps_the_fingerprint_private(self) -> None:
        before = sorted(p.name for p in self.store.app_dir.rglob("*"))
        pv = self.preview("macos")
        self.assertEqual(sorted(p.name for p in self.store.app_dir.rglob("*")), before)
        self.assertEqual(set(pv), {"token", "os", "summary"})

    def test_mismatched_used_or_expired_tokens_are_ignored(self) -> None:
        pv = self.preview("linux")
        other_os = self.api.profiles_create({"name": "W", "os": "windows", "fp_preview": pv["token"]})["data"]
        self.assertNotEqual(other_os["fingerprint_summary"]["user_agent"], pv["summary"]["user_agent"])
        self.assertIn("Windows", other_os["fingerprint_summary"]["user_agent"])
        # The token was consumed by the failed match; it cannot be reused.
        again = self.api.profiles_create({"name": "L", "os": "linux", "fp_preview": pv["token"]})["data"]
        self.assertNotEqual(again["fingerprint_summary"], pv["summary"])

        pv2 = self.preview("linux")
        os_key, fp, _created = self.api._fp_previews[pv2["token"]]
        self.api._fp_previews[pv2["token"]] = (os_key, fp, time.time() - 31 * 60)
        stale = self.api.profiles_create({"name": "S", "os": "linux", "fp_preview": pv2["token"]})["data"]
        self.assertNotEqual(stale["fingerprint_summary"], pv2["summary"])

    def test_screen_pin_must_match_the_preview(self) -> None:
        pv = self.preview("windows", "1920x1080")
        self.assertEqual(pv["summary"]["screen"], "1920x1080")
        row = self.api.profiles_create({"name": "P", "os": "windows", "fp_preview": pv["token"],
                                        "fp_overrides": {"screen": "1920x1080"}})["data"]
        self.assertEqual(row["fingerprint_summary"], pv["summary"])
        pv2 = self.preview("windows", "1920x1080")
        other = self.api.profiles_create({"name": "Q", "os": "windows", "fp_preview": pv2["token"],
                                          "fp_overrides": {"screen": "1366x768"}})["data"]
        self.assertEqual(other["fingerprint_summary"]["screen"], "1366x768")

    def test_token_store_is_capped(self) -> None:
        for _ in range(self.api._PREVIEW_MAX + 5):
            self.preview("macos")
        self.assertLessEqual(len(self.api._fp_previews), self.api._PREVIEW_MAX)
        self.assertFalse(self.api.fingerprint_preview("macos", "banana")["ok"])

    def test_save_rechecks_os_coherence(self) -> None:
        fp = generate_verified_fingerprint("windows")
        with self.assertRaises(ValueError):
            save_fingerprint(self.store.app_dir, "macos", fp)


if __name__ == "__main__":
    unittest.main()
