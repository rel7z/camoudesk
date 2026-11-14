"""Canvas policy (core/host_gpu.py): a profile claiming this Mac shows its real, stable canvas;
any other identity stays covered. Live: launches Camoufox headless."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

import pytest

from camoudesk.core import host_gpu
from camoudesk.core.launcher import profile_camoufox_kwargs, resolve_network
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore

CANVAS_JS = """() => {
  const c = document.createElement('canvas'); c.width = 240; c.height = 60;
  const x = c.getContext('2d'); x.textBaseline = 'top'; x.font = '16px Arial';
  x.fillStyle = '#f60'; x.fillRect(10, 5, 100, 30); x.fillStyle = '#069'; x.fillText('CamouDesk canvas', 4, 20);
  const g = document.createElement('canvas').getContext('webgl');
  const d = g && g.getExtension('WEBGL_debug_renderer_info');
  return { a: c.toDataURL(), b: c.toDataURL(), renderer: d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : null };
}"""


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@pytest.mark.live_browser
@unittest.skipUnless(sys.platform == "darwin", "the real-canvas policy is about identities that claim this Mac")
class TestCanvasPolicyLive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        SettingsStore(self.store.app_dir).update({"search_engine": "none"})
        self.gpu = host_gpu.ensure(self.store.app_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read_canvas(self, profile):  # type: ignore[no-untyped-def]
        from camoufox.sync_api import Camoufox

        kw, _ = profile_camoufox_kwargs(profile, self.store, resolve_network(profile, ProxyStore(self.store.app_dir)), headless=True)
        with Camoufox(**kw) as ctx:
            page = ctx.new_page()
            page.goto("about:blank")
            return kw["canvas_noise"], page.evaluate(CANVAS_JS)

    def test_mac_identity_shows_its_real_stable_canvas(self) -> None:
        if not host_gpu.real_canvas(self.store.app_dir, "macos", False, False):
            self.skipTest(f"this Mac's GPU {self.gpu!r} is not one fpgen records for Firefox on macOS")
        prof = self.store.create_profile({"name": "Mac", "os": "macos"})
        noise, first = self.read_canvas(prof)
        _, second = self.read_canvas(prof)  # also proves no host_gpu deadlock in a persistent launch
        self.assertIsNone(noise)
        self.assertEqual(first["a"], first["b"])  # Firefox never perturbs pixels between reads
        self.assertEqual(_hash(first["a"]), _hash(second["a"]))  # nor between launches
        self.assertEqual(first["renderer"], self.gpu[1])  # claims the GPU that drew the pixels

    def test_other_os_and_gpu_overrides_stay_covered(self) -> None:
        win = self.store.create_profile({"name": "Win", "os": "windows"})
        kw, _ = profile_camoufox_kwargs(win, self.store, resolve_network(win, ProxyStore(self.store.app_dir)), headless=True)
        self.assertIs(kw["canvas_noise"], True)
        self.assertFalse(host_gpu.real_canvas(self.store.app_dir, "macos", True, False))  # a named GPU
        self.assertFalse(host_gpu.real_canvas(self.store.app_dir, "macos", False, True))  # WebGL off


@pytest.mark.live_browser
@unittest.skipUnless(sys.platform == "darwin", "the real-canvas policy is about identities that claim this Mac")
class TestRealDevicePresetLive(TestCanvasPolicyLive):
    def test_mac_preset_with_this_gpu_shows_real_canvas_and_keeps_its_values(self) -> None:
        if not self.gpu:
            self.skipTest("no host GPU")
        from camoudesk.core.fingerprint import generate_preset_fingerprint, save_fingerprint

        prof = self.store.create_profile({"name": "Preset", "os": "macos"})
        try:
            fp = generate_preset_fingerprint("macos", gpu_renderer=self.gpu[1])
        except ValueError:
            self.skipTest("no recorded macOS device with this Mac's GPU")
        save_fingerprint(self.store.profile_dir(prof.id), "macos", fp)
        noise, first = self.read_canvas(prof)
        _, second = self.read_canvas(prof)
        self.assertIs(noise, False)  # Camoufox treats a preset GPU as named; it is this Mac's, so uncovered
        self.assertEqual(first["a"], first["b"])
        self.assertEqual(first["a"], second["a"])
        self.assertEqual(first["renderer"], self.gpu[1])

    def test_mac_identity_shows_its_real_stable_canvas(self) -> None:  # covered by the parent class
        pass

    def test_other_os_and_gpu_overrides_stay_covered(self) -> None:  # covered by the parent class
        pass


class TestCanvasPolicyOffline(unittest.TestCase):
    def test_unknown_host_gpu_keeps_canvases_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(host_gpu.real_canvas(Path(tmp), host_gpu.HOST_OS, False, False))


if __name__ == "__main__":
    unittest.main()
