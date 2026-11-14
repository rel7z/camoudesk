"""The host's GPU, for Camoufox's canvas decision, probed once per Camoufox build in a safe place.

Camoufox (and stock Firefox) never perturbs canvas pixels. When a profile claims this Mac's own OS
and GPU, its real canvas matches its identity, and covering it (canvas noise / the canvas
placeholder) would mark it as a spoofer. Deciding that needs the GPU this Mac's Camoufox reports,
which Camoufox learns by briefly launching the browser (``camoufox.host_rendering.host_gpu``).

That probe must never run inside Camoufox's own launch: it starts a second Playwright from a thread
pool, which deadlocks the Juggler pipes of a persistent context on macOS (CLAUDE.md invariant 2).
So the result is stored in ``host_gpu.json`` per browser binary. ``ensure()`` probes outside any
launch (GUI startup, worker/SDK before ``Camoufox()``), and ``install()`` points Camoufox's probe at
the stored value. The probe runs headless (no window flashes up); on macOS headless Firefox renders
WebGL on the same Metal device as a headed one.
"""

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from camoudesk.core.jsonstore import atomic_write_json, locked, read_json

Gpu = Tuple[str, str]
HOST_OS = "macos" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else "linux"
_FILE = "host_gpu.json"
_mem: Dict[Tuple[str, str], Optional[Gpu]] = {}  # (data folder, binary) -> gpu
_mem_lock = threading.Lock()
_probe_lock = threading.Lock()  # one probe at a time in this process


def browser_binary() -> Optional[str]:
    """The Camoufox executable launches will use, or None when it is not installed."""
    try:
        from camoufox.pkgman import launch_path

        return str(launch_path())
    except Exception:
        return None


def _paths(app_dir: Path) -> Tuple[Path, Path]:
    return app_dir / _FILE, app_dir / "host_gpu.lock"


def cached(app_dir: Path, binary: Optional[str] = None) -> Tuple[bool, Optional[Gpu]]:
    """(known, gpu) without probing. ``known`` with gpu None means WebGL reported no device."""
    binary = binary or browser_binary()
    if not binary:
        return False, None
    key = (str(app_dir), binary)
    with _mem_lock:
        if key in _mem:
            return True, _mem[key]
    path, lock = _paths(app_dir)
    if not path.is_file():  # nothing stored yet; reading must not create files
        return False, None
    try:
        with locked(lock):
            data = read_json(path, {})
    except Exception:
        return False, None
    entry = data.get(binary) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or "gpu" not in entry:
        return False, None
    gpu = tuple(entry["gpu"]) if isinstance(entry["gpu"], list) and len(entry["gpu"]) == 2 else None
    with _mem_lock:
        _mem[key] = gpu  # type: ignore[assignment]
    return True, gpu  # type: ignore[return-value]


def ensure(app_dir: Path, binary: Optional[str] = None) -> Optional[Gpu]:
    """The host GPU, probing it now if it is not stored yet. Call outside any Camoufox launch.

    CAMOUDESK_HOST_GPU=off skips the probe (offline unit tests); canvases then stay covered.
    """
    if os.environ.get("CAMOUDESK_HOST_GPU") == "off":
        return None
    binary = binary or browser_binary()
    if not binary:
        return None
    known, gpu = cached(app_dir, binary)
    if known:
        return gpu
    with _probe_lock:
        known, gpu = cached(app_dir, binary)
        return gpu if known else _probe_and_store(app_dir, binary)


def _probe_and_store(app_dir: Path, binary: str) -> Optional[Gpu]:
    from camoufox.host_rendering import host_gpu as camoufox_host_gpu

    from camoudesk.core.launcher import minimal_env

    import camoufox.utils as cu

    probe = getattr(cu, "_camoudesk_original_host_gpu", camoufox_host_gpu)
    try:
        gpu = probe(binary, True, minimal_env(), {})
    except Exception:
        return None  # unknown: canvases stay covered, which is safe
    path, lock = _paths(app_dir)
    with locked(lock):
        data = read_json(path, {})
        data = data if isinstance(data, dict) else {}
        data[binary] = {"gpu": list(gpu) if gpu else None}
        atomic_write_json(path, data)
    with _mem_lock:
        _mem[(str(app_dir), binary)] = gpu
    return gpu


def install(app_dir: Path) -> None:
    """Make Camoufox's canvas decision use the stored probe instead of launching a browser."""
    import camoufox.utils as cu

    if not hasattr(cu, "_camoudesk_original_host_gpu"):
        cu._camoudesk_original_host_gpu = cu.host_gpu  # type: ignore[attr-defined]

    def stored_host_gpu(executable_path: Any, headless: Any, env: Any, prefs: Any) -> Optional[Gpu]:
        known, gpu = cached(app_dir, str(executable_path))
        if known:
            return gpu
        # Never probe inside a launch. profile_camoufox_kwargs only lets Camoufox decide
        # (canvas_noise=None) once real_canvas() has seen a stored result, so this is not reached.
        return None

    cu.host_gpu = stored_host_gpu  # type: ignore[assignment]


def real_canvas(
    app_dir: Path, profile_os: str, named_gpu: bool, webgl_disabled: bool, claimed_renderer: Optional[str] = None
) -> bool:
    """Whether this profile can show its real canvas: it claims this host's OS, names no other
    GPU, and the host renders on a hardware GPU Firefox is recorded with on this OS. A recorded
    real device (``claimed_renderer``) qualifies only when its GPU is this host's."""
    if profile_os != HOST_OS or named_gpu or webgl_disabled:
        return False
    known, gpu = cached(app_dir)
    if not known or gpu is None:
        return False
    if claimed_renderer is not None and claimed_renderer != gpu[1]:
        return False
    try:
        from camoufox.host_rendering import renders_on_hardware
        from camoufox.webgl import firefox_gpus

        key = {"macos": "mac", "windows": "win", "linux": "lin"}[profile_os]
        return renders_on_hardware(gpu) and tuple(gpu) in {tuple(g) for g in firefox_gpus(key)}
    except Exception:
        return False
