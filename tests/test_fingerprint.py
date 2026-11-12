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
