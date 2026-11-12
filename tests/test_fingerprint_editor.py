"""Fingerprint editor: override sanitising, launch mapping, screen pinning, parity."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from camoudesk.core.launcher import (
    SCREEN_PRESETS,
    build_launch_spec,
    gpu_options,
    profile_camoufox_kwargs,
    resolve_network,
)
from camoudesk.core.storage import ProfileStore
from camoudesk.models.profile import clean_fp_overrides


def _decode(env: dict, prefix: str) -> dict:
    keys = sorted((k for k in env if k.startswith(prefix)), key=lambda k: int(k.rsplit("_", 1)[1]))
    return json.loads("".join(env[k] for k in keys)) if keys else {}


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestOverrides(unittest.TestCase):
    def test_sanitiser_keeps_only_known_valid_values(self) -> None:
        raw = {"webgl": ["Apple", "Apple M1, or similar"], "webrtc": "spoof", "geolocation": "block",
               "screen": "1920x1080", "block_images": 1, "evil": "x", "webrtc_bad": "y"}
        self.assertEqual(clean_fp_overrides(raw), {"webgl": ["Apple", "Apple M1, or similar"], "webrtc": "spoof",
                                                    "geolocation": "block", "screen": "1920x1080", "block_images": True})
        self.assertEqual(clean_fp_overrides({"webrtc": "leak", "geolocation": "x", "screen": "12x9", "webgl": ["only one"]}), {})

    def test_gpu_and_screen_options_exist_for_every_os(self) -> None:
        for os_ in ("macos", "windows", "linux"):
            with self.subTest(os=os_):
                self.assertTrue(gpu_options(os_))
                self.assertTrue(SCREEN_PRESETS[os_])


class TestLaunchMapping(StoreCase):
    def kwargs_for(self, overrides, block_webrtc=True):
        prof = self.store.create_profile({"name": "M", "fp_overrides": overrides, "block_webrtc": block_webrtc})
        net = resolve_network(prof)
        return profile_camoufox_kwargs(prof, self.store, net, headless=True)

    def test_defaults_add_nothing(self) -> None:
        kw, _ = self.kwargs_for({})
        self.assertTrue(kw["block_webrtc"])
        for key in ("webgl_config", "block_images", "block_webgl", "firefox_user_prefs"):
            self.assertNotIn(key, kw)

    def test_overrides_reach_camoufox(self) -> None:
        gpu = gpu_options("macos")[0]
        kw, _ = self.kwargs_for({"webgl": gpu, "block_images": True, "block_webgl": True, "geolocation": "block", "webrtc": "real"})
        self.assertEqual(kw["webgl_config"], tuple(gpu))
        self.assertTrue(kw["block_images"] and kw["block_webgl"])
        self.assertEqual(kw["firefox_user_prefs"], {"permissions.default.geo": 2})
        self.assertFalse(kw["block_webrtc"])
        kw, _ = self.kwargs_for({"geolocation": "prompt"})
        self.assertEqual(kw["firefox_user_prefs"], {"permissions.default.geo": 0})

    def test_spoof_without_geoip_blocks_instead_of_leaking(self) -> None:
        with patch("camoudesk.core.launcher.resolve_geoip", return_value=None):
            kw, _ = self.kwargs_for({"webrtc": "spoof"})
        self.assertTrue(kw["block_webrtc"])
        with patch("camoudesk.core.launcher.resolve_geoip", return_value="203.0.113.5"):
            kw, _ = self.kwargs_for({"webrtc": "spoof"})
        self.assertFalse(kw["block_webrtc"])
        self.assertEqual(kw["geoip"], "203.0.113.5")


class TestScreenAndOsChanges(StoreCase):
    def test_create_and_regenerate_with_pinned_screen(self) -> None:
        prof = self.store.create_profile({"name": "S", "os": "windows", "fp_overrides": {"screen": "1366x768"}})
        self.assertEqual(prof.fingerprint_summary["screen"], "1366x768")
        again = self.store.regenerate_fingerprint(prof.id)  # keeps the pin
        self.assertEqual(again.fingerprint_summary["screen"], "1366x768")
        bigger = self.store.regenerate_fingerprint(prof.id, screen="2560x1440")
        self.assertEqual((bigger.fingerprint_summary["screen"], bigger.fp_overrides["screen"]), ("2560x1440", "2560x1440"))
        free = self.store.regenerate_fingerprint(prof.id, screen="")
        self.assertNotIn("screen", free.fp_overrides)
        with self.assertRaises(ValueError):
            self.store.regenerate_fingerprint(prof.id, screen="banana")

    def test_os_change_drops_gpu_override_and_syncs_webrtc(self) -> None:
        prof = self.store.create_profile({"name": "G", "os": "macos", "fp_overrides": {"webgl": gpu_options("macos")[0], "webrtc": "real"}})
        self.assertFalse(prof.block_webrtc)  # webrtc override and legacy flag stay consistent
        moved = self.store.update_profile(prof.id, {"os": "linux"})
        self.assertNotIn("webgl", moved.fp_overrides)
        cleared = self.store.update_profile(prof.id, {"fp_overrides": {"webrtc": "block", "webgl": None}})
        self.assertTrue(cleared.block_webrtc)


@pytest.mark.live_browser
class TestOverrideParityLive(StoreCase):
    def test_direct_and_worker_identity_match_with_overrides(self) -> None:
        from camoufox.utils import launch_options

        gpu = gpu_options("windows")[2]
        prof = self.store.create_profile({"name": "Parity+", "os": "windows", "timezone": "Europe/Madrid", "locale": "es-ES",
                                          "fp_overrides": {"webgl": gpu, "block_images": True, "geolocation": "prompt", "webrtc": "block"}})
        net = resolve_network(prof)
        spec = build_launch_spec(prof, self.store, network=net)
        kw, _ = profile_camoufox_kwargs(prof, self.store, net, headless=False)
        kw.pop("persistent_context")
        worker = launch_options(pin_cpu_cores=False, **kw)  # what Camoufox(persistent_context=True, **kw) computes
        self.assertEqual(_decode(spec.env, "CAMOU_CONFIG_"), _decode(worker["env"], "CAMOU_CONFIG_"))
        self.assertEqual(_decode(spec.env, "CAMOU_PREFS_"), _decode(worker["env"], "CAMOU_PREFS_"))
        cfg = _decode(spec.env, "CAMOU_CONFIG_")
        self.assertEqual(cfg.get("webGl:renderer"), gpu[1])
        self.assertEqual(_decode(spec.env, "CAMOU_PREFS_").get("permissions.default.geo"), 0)


if __name__ == "__main__":
    unittest.main()
