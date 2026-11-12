"""Profile export/import (.camoudesk archives), including hostile archives."""

import json
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from camoudesk.core import profile_archive as A
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore
from camoudesk.ui.api import DesktopBridgeApi
from test_api_and_manager import FakeBrowserManager


class ArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=self.root / "a", legacy_dir=self.root / "legacy")
        self.proxies = ProxyStore(self.store.app_dir)
        # A second, independent install ("the other Mac").
        self.store2 = ProfileStore(app_dir=self.root / "b", legacy_dir=self.root / "legacy")
        self.proxies2 = ProxyStore(self.store2.app_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_profile(self, **data):  # type: ignore[no-untyped-def]
        prof = self.store.create_profile({"name": "Shop", "folder": "Stores", "tags": ["vip"], "os": "windows",
                                          "fp_overrides": {"webrtc": "real"}, **data})
        ff = self.store.firefox_data_dir(prof.id)
        (ff / "cookies.sqlite").write_text("COOKIES")
        (ff / "storage").mkdir()
        (ff / "storage" / "ls.sqlite").write_text("LOCALSTORAGE")
        (ff / "cache2").mkdir()
        (ff / "cache2" / "entry").write_text("CACHE")
        (ff / ".parentlock").write_text("")
        return prof

    def names_in(self, archive: Path):  # type: ignore[no-untyped-def]
        with zipfile.ZipFile(archive) as zf:
            return set(zf.namelist())


class TestRoundTrip(ArchiveCase):
    def test_export_import_moves_identity_and_data(self) -> None:
        prof = self.make_profile(proxy="socks5://alice:s3cret@10.0.0.1:1080")
        out = self.root / "shop.camoudesk"
        res = A.export_profile(self.store, self.proxies, prof.id, out)
        self.assertEqual(res["path"], str(out))
        names = self.names_in(out)
        self.assertTrue({"manifest.json", "fingerprint.json", "firefox_data/cookies.sqlite", "firefox_data/storage/ls.sqlite"} <= names)
        self.assertFalse(any("cache2" in n or "parentlock" in n for n in names))
        manifest = json.loads(zipfile.ZipFile(out).read("manifest.json"))
        self.assertNotIn("s3cret", json.dumps(manifest))  # password excluded by default
        self.assertFalse(manifest["includes_credentials"])

        imported = A.import_profile(self.store2, self.proxies2, out)
        self.assertNotEqual(imported.id, prof.id)
        self.assertEqual((imported.name, imported.os, imported.folder, imported.tags, imported.fp_overrides),
                         ("Shop", "windows", "Stores", ["vip"], {"webrtc": "real"}))
        self.assertEqual((self.store2.profile_dir(imported.id) / "fingerprint.json").read_bytes(),
                         (self.store.profile_dir(prof.id) / "fingerprint.json").read_bytes())
        self.assertEqual((self.store2.firefox_data_dir(imported.id) / "cookies.sqlite").read_text(), "COOKIES")
        self.assertEqual(imported.proxy, "socks5://alice@10.0.0.1:1080")  # user kept, password must be re-entered

    def test_credentials_only_when_asked_and_name_collision(self) -> None:
        prof = self.make_profile(proxy="http://bob:pw@10.0.0.2:8080")
        out = self.root / "with.camoudesk"
        A.export_profile(self.store, self.proxies, prof.id, out, include_credentials=True)
        first = A.import_profile(self.store, self.proxies, out)  # same install: name collides
        self.assertEqual(first.name, "Shop (imported)")
        self.assertIn("bob:pw@", first.proxy)

    def test_saved_proxy_travels_and_is_reused(self) -> None:
        px = self.proxies.create({"name": "US 1", "type": "http", "host": "10.0.0.9", "port": 3128, "username": "u", "password": "p"})
        prof = self.make_profile(proxy_id=px.id)
        out = self.root / "saved.camoudesk"
        A.export_profile(self.store, self.proxies, prof.id, out)
        imported = A.import_profile(self.store2, self.proxies2, out)
        created = self.proxies2.get(imported.proxy_id)
        self.assertEqual((created.name, created.host, created.username, created.password), ("US 1", "10.0.0.9", "u", ""))
        again = A.import_profile(self.store2, self.proxies2, out)
        self.assertEqual(again.proxy_id, imported.proxy_id)  # identical proxy reused, not duplicated
        self.assertEqual(len(self.proxies2.list()), 1)


class TestHostileArchives(ArchiveCase):
    def write_zip(self, entries, manifest=None, symlink=None):  # type: ignore[no-untyped-def]
        path = self.root / "evil.camoudesk"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest or {"format": A.FORMAT, "version": 1, "profile": {"name": "X", "os": "macos"}}))
            for name, data in entries:
                zf.writestr(name, data)
            if symlink:
                info = zipfile.ZipInfo(symlink)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "/etc/passwd")
        return path

    def assert_rejected(self, path: Path) -> None:
        before = set(self.store2.profiles_dir.iterdir())
        count = len(self.store2.list_profiles())
        with self.assertRaises(A.ArchiveError):
            A.import_profile(self.store2, self.proxies2, path)
        self.assertEqual(set(self.store2.profiles_dir.iterdir()), before)  # no leftovers
        self.assertEqual(len(self.store2.list_profiles()), count)

    def test_rejects_path_escapes_and_unexpected_files(self) -> None:
        for name in ("../escape.txt", "firefox_data/../../escape.txt", "/etc/evil", "other/file", "firefox_data\\..\\x"):
            with self.subTest(name=name):
                self.assert_rejected(self.write_zip([(name, "x")]))
        self.assertFalse((self.root / "escape.txt").exists())

    def test_rejects_symlinks(self) -> None:
        self.assert_rejected(self.write_zip([], symlink="firefox_data/link"))

    def test_rejects_bad_manifests_and_non_zips(self) -> None:
        self.assert_rejected(self.write_zip([], manifest={"format": "something-else", "profile": {}}))
        self.assert_rejected(self.write_zip([], manifest={"format": A.FORMAT, "version": 99, "profile": {"name": "X"}}))
        bogus = self.root / "notzip.camoudesk"
        bogus.write_text("hello")
        self.assert_rejected(bogus)


class TestBridgeGuards(ArchiveCase):
    def test_export_refused_while_running(self) -> None:
        manager = FakeBrowserManager(store=self.store, proxies=self.proxies)
        api = DesktopBridgeApi(self.store, manager, self.proxies, SettingsStore(self.store.app_dir))
        prof = self.make_profile()
        manager.launch(prof.id)
        try:
            with self.assertRaises(ValueError):
                api._export_one(prof.id, self.root / "x.camoudesk", False)
        finally:
            manager.stop_all(timeout=1.0)
        out = self.root / "ok.camoudesk"
        A.export_profile(self.store, self.proxies, prof.id, out)
        res = api.profiles_import_path(str(out))["data"]
        self.assertEqual((res["imported"], res["errors"]), (1, []))


if __name__ == "__main__":
    unittest.main()
