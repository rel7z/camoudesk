"""Keep one CamouDesk window per data folder.

Two running apps would each adopt and launch the same profiles, bind the same proxy-forwarder
ports and stop every browser when either quits. The first app holds an exclusive, non-blocking
``flock`` on ``<data dir>/gui.lock`` (released automatically when it exits, even on a crash);
a second launch brings the first one to the front and exits.
"""

import fcntl
import os
from pathlib import Path
from typing import IO, Optional


class InstanceLock:
    def __init__(self, app_dir: Path) -> None:
        self.path = Path(app_dir) / "gui.lock"
        self._fh: Optional[IO[str]] = None

    def acquire(self) -> bool:
        """True if this process is now the only app instance for the data folder."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh  # held for the life of the process
        return True

    def holder_pid(self) -> Optional[int]:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def activate_existing(pid: Optional[int]) -> bool:
    """Bring the already-running CamouDesk to the front."""
    if not pid:
        return False
    try:
        import AppKit

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return bool(app and app.activateWithOptions_(AppKit.NSApplicationActivateAllWindows))
    except Exception:
        return False
