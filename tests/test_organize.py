"""Phase 6: trash/restore/purge, clone/duplicate, bulk folders and tags, folder rename."""

from pathlib import Path
import tempfile
import unittest

from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.ui.api import DesktopBridgeApi
from test_api_and_manager import FakeBrowserManager


class OrganizeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.proxies = ProxyStore(self.store.app_dir)
        self.manager = FakeBrowserManager(store=self.store, proxies=self.proxies)
        self.api = DesktopBridgeApi(self.store, self.manager, self.proxies, SettingsStore(self.store.app_dir))

    def tearDown(self) -> None:
        self.manager.stop_all(timeout=1.0)
        self.tmp.cleanup()

    def create(self, **data):  # type: ignore[no-untyped-def]
        return self.api.profiles_create({"name": "P", **data})["data"]


class TestTrash(OrganizeCase):
    def test_trash_restore_and_purge(self) -> None:
        a, b = self.create(name="A")["id"], self.create(name="B")["id"]
        self.manager.launch(a)
        self.assertEqual(self.api.profiles_trash([a, b])["data"], {"trashed": 2})
        self.assertEqual(self.manager.get_status(a)["status"], "stopped")  # running browser closed first
        self.assertNotIn(a, [p["id"] for p in self.api.profiles_list()["data"]])
        self.assertEqual(sorted(p["id"] for p in self.api.profiles_trash_list()["data"]), sorted([a, b]))
        self.assertTrue(self.store.profile_dir(a).exists())  # data kept while in the trash

        launch = self.api.profiles_launch(a)
        self.assertFalse(launch["ok"])
        self.assertIn("trash", launch["error"])

        self.api.profiles_restore([a])
        self.assertIn(a, [p["id"] for p in self.api.profiles_list()["data"]])
        # Only trashed profiles can be purged.
        self.assertEqual(self.api.profiles_purge([a])["data"], {"purged": 0})
        self.assertTrue(self.store.profile_dir(a).exists())
        self.assertEqual(self.api.profiles_empty_trash()["data"], {"purged": 1})
        self.assertFalse(self.store.profile_dir(b).exists())
        self.assertIsNone(self.store.get_profile(b))

    def test_trashed_profiles_do_not_count_as_proxy_users(self) -> None:
        px = self.api.proxies_create({"type": "http", "host": "10.0.0.1", "port": 80})["data"]
        pid = self.create(proxy_mode="saved", proxy_id=px["id"])["id"]
        self.assertEqual(self.api.proxies_list()["data"][0]["used_by"], 1)
        self.api.profiles_trash([pid])
        self.assertEqual(self.api.proxies_list()["data"][0]["used_by"], 0)


class TestClone(OrganizeCase):
    def test_clone_settings_gets_new_fingerprint_and_no_data(self) -> None:
        src = self.create(name="Src", os="windows", folder="Shops", tags=["vip"], notes="n",
                          start_urls=["https://a.io", "https://b.io"], fp_overrides={"screen": "1366x768", "webrtc": "real"})
        (self.store.firefox_data_dir(src["id"]) / "cookies.sqlite").write_text("cookies")
        clone = self.api.profiles_clone(src["id"], False, "")["data"]
        self.assertEqual((clone["name"], clone["os"], clone["folder"], clone["tags"], clone["start_urls"]),
                         ("Src (copy)", "windows", "Shops", ["vip"], ["https://a.io", "https://b.io"]))
        self.assertEqual(clone["fingerprint_summary"]["screen"], "1366x768")  # pin honoured
        self.assertEqual(clone["fp_overrides"]["webrtc"], "real")
        self.assertNotEqual((self.store.profile_dir(clone["id"]) / "fingerprint.json").read_bytes(),
                            (self.store.profile_dir(src["id"]) / "fingerprint.json").read_bytes())
        self.assertFalse((self.store.firefox_data_dir(clone["id"]) / "cookies.sqlite").exists())

    def test_duplicate_copies_identity_and_data_but_not_caches(self) -> None:
        src = self.create(name="Src", fp_overrides={"screen": "1710x1107"})
        ff = self.store.firefox_data_dir(src["id"])
        (ff / "cookies.sqlite").write_text("cookies")
        (ff / "cache2").mkdir()
        (ff / "cache2" / "big").write_text("x" * 1000)
        (ff / ".parentlock").write_text("")
        dup = self.api.profiles_clone(src["id"], True, "Twin")["data"]
        self.assertEqual(dup["name"], "Twin")
        self.assertEqual((self.store.profile_dir(dup["id"]) / "fingerprint.json").read_bytes(),
                         (self.store.profile_dir(src["id"]) / "fingerprint.json").read_bytes())
        self.assertEqual(dup["fp_overrides"].get("screen"), "1710x1107")
        dff = self.store.firefox_data_dir(dup["id"])
        self.assertEqual((dff / "cookies.sqlite").read_text(), "cookies")
        self.assertFalse((dff / "cache2").exists())
        self.assertFalse((dff / ".parentlock").exists())

    def test_duplicate_refused_while_running(self) -> None:
        pid = self.create()["id"]
        self.manager.launch(pid)
        res = self.api.profiles_clone(pid, True, "")
        self.assertFalse(res["ok"])
        self.assertTrue(self.api.profiles_clone(pid, False, "")["ok"])  # settings-only clone is fine


class TestBulkMeta(OrganizeCase):
    def test_folders_and_tags_in_bulk_even_while_running(self) -> None:
        a, b = self.create(name="A", tags=["old"])["id"], self.create(name="B")["id"]
        self.manager.launch(a)
        res = self.api.profiles_bulk_meta([a, b], {"folder": "Shops", "add_tags": ["vip", "vip"], "remove_tags": ["old"]})["data"]
        self.assertEqual(res, {"changed": 2})
        for pid in (a, b):
            prof = self.store.get_profile(pid)
            self.assertEqual((prof.folder, prof.tags), ("Shops", ["vip"]))
        self.assertEqual(self.api.folders_rename("Shops", "Stores")["data"], {"changed": 2})
        self.assertEqual(self.store.get_profile(a).folder, "Stores")
        self.api.profiles_bulk_meta([a], {"folder": ""})
        self.assertEqual(self.store.get_profile(a).folder, "")
        self.assertFalse(self.api.folders_rename("", "x")["ok"])


if __name__ == "__main__":
    unittest.main()
