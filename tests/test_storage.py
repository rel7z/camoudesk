"""Unit tests for ProfileStore, atomic persistence, and legacy migration."""

import json
from pathlib import Path
import tempfile
import unittest

from camoudesk.core.storage import ProfileStore
from camoudesk.models.profile import sanitize_profile_id


class TestProfileStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app_dir = self.root / "app_support"
        self.legacy_dir = self.root / "legacy"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_starter_profile_created_when_empty(self) -> None:
        store = ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        profiles = store.list_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].id, "prof_default_1")
        self.assertTrue((store.profile_dir("prof_default_1") / "fingerprint.json").is_file())
        self.assertTrue(store.firefox_data_dir("prof_default_1").is_dir())

    def test_legacy_migration_imports_valid_profiles(self) -> None:
        self.legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_data = [
            {
                "id": "legacy_acc_1",
                "name": "Legacy US Account",
                "os": "windows",
                "proxy": "http://u:p@127.0.0.1:9000",
                "timezone": "America/New_York",
                "locale": "en-US",
                "target_url": "https://example.org",
            },
            {"id": "../escape_attempt", "name": "Bad"},
        ]
        (self.legacy_dir / "profiles_registry.json").write_text(json.dumps(legacy_data), encoding="utf-8")

        store = ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        profiles = store.list_profiles()
        self.assertEqual([p.id for p in profiles], ["legacy_acc_1"])
        self.assertEqual(profiles[0].startup_url, "https://example.org")
        self.assertTrue(profiles[0].fingerprint_summary.get("screen"))

    def test_create_update_delete_and_wipe(self) -> None:
        store = ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        created = store.create_profile(
            {
                "name": "Shop Account",
                "os": "linux",
                "proxy": "socks5://127.0.0.1:1080",
                "timezone": "Europe/London",
                "notes": "Test memo",
            }
        )
        self.assertEqual(created.os, "linux")
        self.assertEqual(created.proxy, "socks5://127.0.0.1:1080")

        updated = store.update_profile(created.id, {"name": "Shop Updated", "block_webrtc": False})
        self.assertEqual(updated.name, "Shop Updated")
        self.assertFalse(updated.block_webrtc)

        pdir = store.profile_dir(created.id)
        self.assertTrue(pdir.is_dir())
        self.assertTrue(store.delete_profile(created.id, wipe_data=True))
        self.assertFalse(pdir.exists())
        self.assertIsNone(store.get_profile(created.id))

    def test_path_traversal_rejected(self) -> None:
        for bad in ("../etc", "foo/bar", "", "a b", ".hidden"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sanitize_profile_id(bad)


if __name__ == "__main__":
    unittest.main()
