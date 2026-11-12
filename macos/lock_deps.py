"""Write macos/requirements.lock: the exact runtime dependency tree of CamouDesk, as installed in
the current environment (run with the dev venv's python after `pip install -e .`).

    .venv/bin/python macos/lock_deps.py

Only packages reachable from camoudesk's own requirements (extras included, markers evaluated for
this Mac) are pinned, so dev tools such as pytest never end up in the app bundle.
"""

from importlib import metadata
from pathlib import Path
import re
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = "camoudesk"
OUT = Path(__file__).resolve().parent / "requirements.lock"


def root_requirements() -> list:
    """Read from pyproject.toml, not the installed metadata (which may predate an edit)."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    return [Requirement(d) for d in deps]


def runtime_closure() -> dict:
    pinned: dict = {}
    queue = [(r.name, frozenset(r.extras)) for r in root_requirements()
             if r.marker is None or r.marker.evaluate({"extra": ""})]
    seen = set()
    while queue:
        name, extras = queue.pop()
        key = (canonicalize_name(name), extras)
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            sys.exit(f"{name} is required but not installed in this environment")
        if canonicalize_name(name) != ROOT:
            pinned[canonicalize_name(name)] = (dist.metadata["Name"], dist.version)
        for raw in dist.requires or []:
            req = Requirement(raw)
            envs = [{"extra": e} for e in extras] or [{"extra": ""}]
            if req.marker is not None and not any(req.marker.evaluate(env) for env in envs):
                continue
            queue.append((req.name, frozenset(req.extras)))
    return pinned


def main() -> None:
    pinned = runtime_closure()
    lines = [
        "# Pinned runtime dependencies for the CamouDesk app bundle (macOS arm64).",
        "# Regenerate after changing dependencies: .venv/bin/python macos/lock_deps.py",
    ]
    lines += [f"{name}=={version}" for _, (name, version) in sorted(pinned.items())]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({len(pinned)} packages)")


if __name__ == "__main__":
    main()
