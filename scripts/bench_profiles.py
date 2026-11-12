"""Benchmark per-profile launch latency and memory footprint.

Launches N headless profiles in an isolated CAMOUDESK_HOME, measures the time until each
browser requests its startup URL from a loopback server, then sums the RSS of every process
in each profile's tree (worker Python, Playwright driver, Firefox and its content processes).

Usage:
    python scripts/bench_profiles.py --count 3 --mode worker
    python scripts/bench_profiles.py --count 3 --mode direct
    python scripts/bench_profiles.py --count 3 --mode direct --headed   # opens windows
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _start_probe_server(hits: Dict[str, float]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            key = self.path.rsplit("/", 1)[-1]
            hits.setdefault(key, time.monotonic())
            body = b"<html><body>bench</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _process_table() -> List[tuple[int, int, int, str]]:
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,rss=,comm="], capture_output=True, text=True, check=True
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    return rows


def _classify(comm: str) -> str:
    name = os.path.basename(comm).lower()
    if "python" in name:
        return "python"
    if name == "node" or name.endswith("/node"):
        return "node"
    if "camoufox" in name or "firefox" in name or "plugin-container" in name or "content" in name:
        return "browser"
    return "other"


def tree_rss_mb(root_pid: int) -> Dict[str, float]:
    rows = _process_table()
    children: Dict[int, List[int]] = {}
    info = {}
    for pid, ppid, rss, comm in rows:
        children.setdefault(ppid, []).append(pid)
        info[pid] = (rss, comm)
    totals: Dict[str, float] = {"python": 0.0, "node": 0.0, "browser": 0.0, "other": 0.0}
    count = 0
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in info:
            rss, comm = info[pid]
            totals[_classify(comm)] += rss / 1024.0
            count += 1
        stack.extend(children.get(pid, []))
    totals["total"] = sum(v for k, v in totals.items() if k != "total")
    totals["processes"] = count
    return totals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--mode", default="worker", choices=("worker", "direct"))
    parser.add_argument("--settle", type=float, default=6.0, help="Seconds to wait after load before sampling RSS")
    parser.add_argument("--samples", type=int, default=5, help="RSS samples (1s apart); the median is reported")
    parser.add_argument("--headed", action="store_true", help="Open real browser windows instead of headless")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    home = tempfile.mkdtemp(prefix="camoudesk-bench-")
    os.environ["CAMOUDESK_HOME"] = home
    os.environ["CAMOUDESK_LEGACY_HOME"] = os.path.join(home, "no-legacy")

    from camoudesk.core.storage import ProfileStore
    from camoudesk.manager.process_manager import ProcessManager

    hits: Dict[str, float] = {}
    server = _start_probe_server(hits)
    port = server.server_address[1]

    store = ProfileStore()
    manager = ProcessManager(store=store)
    supports_mode = "mode" in inspect.signature(manager.launch).parameters

    ids = []
    for i in range(args.count):
        prof = store.create_profile({"name": f"bench-{i}", "os": "macos"})
        store.update_profile(prof.id, {"startup_url": f"http://127.0.0.1:{port}/p/{prof.id}"})
        ids.append(prof.id)

    started: Dict[str, float] = {}
    pids: Dict[str, int] = {}
    results: Dict[str, Dict[str, float]] = {}
    try:
        for pid_key in ids:
            started[pid_key] = time.monotonic()
            extra = {"mode": args.mode} if supports_mode else {}
            res = manager.launch(pid_key, headless=not args.headed, **extra)
            if not res.get("ok"):
                print(f"launch failed for {pid_key}: {res}", file=sys.stderr)
                return 1
            pids[pid_key] = int(res["pid"])

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not all(k in hits for k in ids):
            time.sleep(0.1)
        missing = [k for k in ids if k not in hits]
        if missing:
            for k in missing:
                print(f"--- {k} never loaded; log tail:\n{manager.read_logs(k, 30)}", file=sys.stderr)
            return 1

        time.sleep(args.settle)
        samples: Dict[str, List[Dict[str, float]]] = {k: [] for k in ids}
        for _ in range(args.samples):
            for k in ids:
                samples[k].append(tree_rss_mb(pids[k]))
            time.sleep(1.0)
        for k in ids:
            # Median sample by total RSS: startup transients make single readings noisy.
            ordered = sorted(samples[k], key=lambda m: m["total"])
            mem = dict(ordered[len(ordered) // 2])
            mem["launch_to_first_request_s"] = round(hits[k] - started[k], 2)
            results[k] = {key: round(val, 1) if isinstance(val, float) else val for key, val in mem.items()}
    finally:
        manager.stop_all()
        server.shutdown()

    summary = {
        "mode": args.mode if supports_mode else "worker (legacy)",
        "count": args.count,
        "per_profile": results,
        "avg_total_mb": round(sum(r["total"] for r in results.values()) / len(results), 1),
        "avg_launch_s": round(sum(r["launch_to_first_request_s"] for r in results.values()) / len(results), 2),
        "home": home,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
