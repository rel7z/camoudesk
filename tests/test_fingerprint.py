"""Tests for deterministic fingerprint freezing and regeneration."""

from pathlib import Path
import tempfile
import unittest

from camoudesk.core.fingerprint import (
    ensure_profile_fingerprint,
    generate_and_save_fingerprint,
    load_saved_fingerprint,
)


class TestFingerprintPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.profile_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fingerprint_frozen_and_reloaded_deterministically(self) -> None:
        fp1, summary1 = ensure_profile_fingerprint(self.profile_dir, "macos")
        fp2, summary2 = ensure_profile_fingerprint(self.profile_dir, "macos")

        self.assertEqual(fp1["navigator"]["userAgent"], fp2["navigator"]["userAgent"])
        self.assertEqual(fp1["screen"], fp2["screen"])
        self.assertEqual(summary1.screen, summary2.screen)
        self.assertIn("Mac", fp1["navigator"]["platform"])

    def test_os_change_triggers_coherent_regeneration(self) -> None:
        ensure_profile_fingerprint(self.profile_dir, "macos")
        fp_win, _ = ensure_profile_fingerprint(self.profile_dir, "windows")
        self.assertIn("Win", fp_win["navigator"]["platform"])

    def test_explicit_regeneration_replaces_disk_file(self) -> None:
        generate_and_save_fingerprint(self.profile_dir, "linux")
        loaded = load_saved_fingerprint(self.profile_dir)
        self.assertIsNotNone(loaded)
        self.assertIn("Linux", loaded["navigator"]["platform"])


if __name__ == "__main__":
    unittest.main()


class TestOsCoherence(unittest.TestCase):
    def test_every_navigator_signal_must_match_the_os(self) -> None:
        from camoudesk.core.fingerprint import fingerprint_matches_os

        linux_ua = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"
        win_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
        good = {"navigator": {"platform": "Linux x86_64", "oscpu": "Linux x86_64", "userAgent": linux_ua}}
        # Seen from fpgen ~1-2% of the time for os="linux": Linux platform, Windows oscpu.
        mixed = {"navigator": {"platform": "Linux x86_64", "oscpu": "Windows NT 10.0; Win64; x64", "userAgent": linux_ua}}
        windows = {"navigator": {"platform": "Win32", "oscpu": "Windows NT 10.0; Win64; x64", "userAgent": win_ua}}
        self.assertTrue(fingerprint_matches_os(good, "linux"))
        self.assertFalse(fingerprint_matches_os(mixed, "linux"))
        self.assertFalse(fingerprint_matches_os(windows, "linux"))
        self.assertTrue(fingerprint_matches_os(windows, "windows"))

    def test_generated_fingerprints_are_coherent(self) -> None:
        from camoudesk.core.fingerprint import fingerprint_matches_os

        with tempfile.TemporaryDirectory() as tmp:
            for os_ in ("linux", "windows", "macos"):
                for _ in range(15):
                    fp, _ = generate_and_save_fingerprint(Path(tmp), os_)
                    self.assertTrue(fingerprint_matches_os(fp, os_), fp["navigator"])
