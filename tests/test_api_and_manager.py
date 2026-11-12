"""Unit tests for ProcessManager and DesktopBridgeApi."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.ui.api import DesktopBridgeApi


class TestProcessManagerAndApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=self.root / "app", legacy_dir=self.root / "legacy")
        self.manager = ProcessManager(store=self.store)
        self.api = DesktopBridgeApi(store=self.store, manager=self.manager)

    def tearDown(self) -> None:
        self.manager.stop_all()
        self.tmp.cleanup()

    @patch("camoudesk.manager.process_manager.subprocess.Popen")
    def test_launch_logs_status_and_edit_guard(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 4242
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        created = self.api.create_profile({"name": "Worker Test", "os": "macos", "proxy": "http://u:secret@127.0.0.1:8888"})
        self.assertTrue(created["ok"])
        pid = created["data"]["id"]
        self.assertNotIn("secret", created["data"]["proxy_masked"])

        launched = self.api.launch_profile(pid)
        self.assertTrue(launched["ok"])
        self.assertEqual(launched["pid"], 4242)
        self.assertEqual(mock_popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

        # Editing while running must be rejected safely
        edit_blocked = self.api.update_profile(pid, {"name": "Blocked Rename"})
        self.assertFalse(edit_blocked["ok"])

        listed = self.api.list_profiles()
        target_row = next(r for r in listed["data"] if r["id"] == pid)
        self.assertEqual(target_row["status"], "running")

        stopped = self.api.stop_profile(pid)
        self.assertTrue(stopped["ok"])
        mock_proc.terminate.assert_called_once()

        # Editing after stop succeeds
        edit_ok = self.api.update_profile(pid, {"name": "Renamed Cleanly"})
        self.assertTrue(edit_ok["ok"])
        self.assertEqual(edit_ok["data"]["name"], "Renamed Cleanly")

    @patch("camoudesk.manager.process_manager.subprocess.Popen")
    def test_crashed_worker_surfaces_log_tail(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        # First poll inside launch not called; later poll simulates non-zero exit
        mock_proc.poll.return_value = 1
        mock_popen.return_value = mock_proc

        prof_id = self.store.list_profiles()[0].id
        self.api.launch_profile(prof_id)
        # Simulate worker output written to log file
        log_path = self.store.log_file(prof_id)
        log_path.write_text("[ERROR] Proxy connection refused\n", encoding="utf-8")

        st = self.manager.get_status(prof_id)
        self.assertEqual(st["status"], "stopped")
        self.assertIn("Proxy connection refused", st["last_error"])

        logs_res = self.api.get_profile_logs(prof_id)
        self.assertTrue(logs_res["ok"])
        self.assertIn("Proxy connection refused", logs_res["data"])


if __name__ == "__main__":
    unittest.main()
