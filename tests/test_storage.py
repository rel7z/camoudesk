"""Unit tests for ProfileStore, atomic persistence, and legacy migration."""

import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from camoudesk.core.storage import ProfileStore, RegistryCorruptError
from camoudesk.models.profile import sanitize_profile_id


def _create_many(app_dir: str, legacy_dir: str, worker: int, count: int) -> None:
    store = ProfileStore(app_dir=Path(app_dir), legacy_dir=Path(legacy_dir))
    for i in range(count):
        prof = store.create_profile({"name": f"w{worker}-{i}"})
        store.mark_used(prof.id)


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

    def test_corrupt_registry_is_never_overwritten(self) -> None:
        store = ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        store.registry_path.write_text("[{truncated", encoding="utf-8")

        with self.assertRaises(RegistryCorruptError):
            store.create_profile({"name": "Would clobber"})
        with self.assertRaises(RegistryCorruptError):
            store.list_profiles()

        self.assertEqual(store.registry_path.read_text(encoding="utf-8"), "[{truncated")
        backups = list(store.registry_path.parent.glob("registry.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        # Re-opening the store must not reseed over the corrupt file either.
        ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        self.assertEqual(store.registry_path.read_text(encoding="utf-8"), "[{truncated")

    def test_invalid_entry_survives_unrelated_writes(self) -> None:
        store = ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        good = store.create_profile({"name": "Good"})
        raw = json.loads(store.registry_path.read_text(encoding="utf-8"))
        bad_entry = {"id": "hand_edited", "name": "Bad proxy", "proxy": "http://hostonly", "future_field": 1}
        raw.append(bad_entry)
        store.registry_path.write_text(json.dumps(raw), encoding="utf-8")

        self.assertNotIn("hand_edited", [p.id for p in store.list_profiles()])
        store.mark_used(good.id)
        store.create_profile({"name": "Another"})

        persisted = json.loads(store.registry_path.read_text(encoding="utf-8"))
        self.assertIn(bad_entry, persisted)

    def test_proxy_exit_ip_cleared_when_proxy_changes(self) -> None:
        store = ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        prof = store.create_profile({"name": "Geo", "proxy": "socks5://10.0.0.1:1080", "proxy_exit_ip": "203.0.113.7"})
        self.assertEqual(prof.proxy_exit_ip, "203.0.113.7")
        self.assertEqual(store.update_profile(prof.id, {"name": "Geo2"}).proxy_exit_ip, "203.0.113.7")
        self.assertEqual(store.update_profile(prof.id, {"proxy": "socks5://10.0.0.2:1080"}).proxy_exit_ip, "")
        self.assertEqual(store.update_profile(prof.id, {"proxy_exit_ip": "not-an-ip"}).proxy_exit_ip, "")

    def test_concurrent_processes_do_not_lose_writes(self) -> None:
        ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir)
        ctx = multiprocessing.get_context("spawn")
        workers, per_worker = 4, 6
        procs = [
            ctx.Process(target=_create_many, args=(str(self.app_dir), str(self.legacy_dir), w, per_worker))
            for w in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)

        names = [p.name for p in ProfileStore(app_dir=self.app_dir, legacy_dir=self.legacy_dir).list_profiles()]
        self.assertEqual(len(names), 1 + workers * per_worker)

    def test_path_traversal_rejected(self) -> None:
        for bad in ("../etc", "foo/bar", "", "a b", ".hidden"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sanitize_profile_id(bad)


if __name__ == "__main__":
    unittest.main()
