"""Shared primitives for CamouDesk's JSON files: atomic writes and a process+thread lock."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict, Iterator

_thread_locks: Dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    """Write JSON to a temp file in the same directory and atomically rename it over ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=indent)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Exclusive lock shared by threads in this process and by other processes (flock)."""
    key = str(lock_path)
    with _thread_locks_guard:
        tlock = _thread_locks.setdefault(key, threading.RLock())
    with tlock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class CorruptFileError(RuntimeError):
    """A JSON store exists but cannot be parsed; it is never overwritten."""


def read_json(path: Path, default: Any) -> Any:
    """Read JSON, returning ``default`` when the file is missing; raise on corruption."""
    if not path.is_file():
        return default
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorruptFileError(f"{path} is unreadable ({exc}); CamouDesk will not overwrite it.") from exc
