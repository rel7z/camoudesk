"""Launch modes and the worker environment shared by the manager modules."""

import os
from pathlib import Path
from typing import Dict

from camoudesk.core.launcher import minimal_env

MODE_DIRECT = "direct"
MODE_WORKER = "worker"
MODE_SDK = "sdk"  # a user script using camoudesk.sdk; tracked, never launched by the manager
_MODES = (MODE_DIRECT, MODE_WORKER)


def _build_worker_env() -> Dict[str, str]:
    """Minimal environment for the worker (and the browser it starts) that can import camoudesk.

    Secrets in the GUI's environment never reach workers or browsers; only the allowlist,
    CamouDesk's own settings and Python's search path are passed on.
    """
    env = minimal_env(extra_prefixes=("CAMOUDESK_", "PYTHONPATH", "PYTHONUTF8"))
    src_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if src_root not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src_root, *parts])
    return env

