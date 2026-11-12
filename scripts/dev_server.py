"""Serve the frontend for UI development (open /index.html?mock). Sends no-store so edits show up
on reload; Python's plain http.server lets browsers cache ES modules between edits."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

FRONTEND = Path(__file__).resolve().parents[1] / "src" / "camoudesk" / "ui" / "frontend"


class NoCacheHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        # Never answer 304: a stale module cached earlier must be replaced, not revalidated.
        del self.headers["If-Modified-Since"]
        del self.headers["If-None-Match"]
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *_args: object) -> None:
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5178
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(NoCacheHandler, directory=str(FRONTEND)))
    print(f"Frontend dev server: http://127.0.0.1:{port}/index.html?mock", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
