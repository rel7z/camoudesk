"""Local REST API for scripts and other tools (off by default; enable it in Settings).

Bound to 127.0.0.1 only. Every request needs ``Authorization: Bearer <token>`` (Settings shows
and rotates the token). The Host header must name the loopback address, which blocks DNS
rebinding from web pages; no CORS headers are sent, so browser pages cannot read responses.

Endpoints (JSON in and out; errors are ``{"error": "..."}`` with a 4xx status)::

    GET    /v1/health
    GET    /v1/profiles                      list (trash excluded)
    POST   /v1/profiles                      create   {name, os, proxy_mode, ...}
    GET    /v1/profiles/{id}
    PATCH  /v1/profiles/{id}                 update
    POST   /v1/profiles/{id}/start           {"headless": false}
    POST   /v1/profiles/{id}/stop
    GET    /v1/profiles/{id}/status
    GET    /v1/proxies
    POST   /v1/proxies/{id}/check

Full browser automation of a saved profile runs in your own script through the Python SDK
(``camoudesk.sdk.open_profile``): Playwright cannot serve a persistent profile over a socket.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from camoudesk import __version__

MAX_BODY = 1024 * 1024
_ID = r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ApiServer:
    def __init__(self, bridge: Any, port: int, token_provider: Callable[[], str]) -> None:
        self.bridge = bridge
        self.port = int(port)
        self._token = token_provider
        self._httpd: Optional[_Server] = None
        self._routes = [
            ("GET", re.compile(r"^/v1/health$"), self._health),
            ("GET", re.compile(r"^/v1/profiles$"), lambda m, b: self._call("profiles_list")),
            ("POST", re.compile(r"^/v1/profiles$"), lambda m, b: self._call("profiles_create", b, created=True)),
            ("GET", re.compile(rf"^/v1/profiles/{_ID}$"), lambda m, b: self._call("profiles_get", m[1])),
            ("PATCH", re.compile(rf"^/v1/profiles/{_ID}$"), lambda m, b: self._call("profiles_update", m[1], b)),
            ("POST", re.compile(rf"^/v1/profiles/{_ID}/start$"), self._start),
            ("POST", re.compile(rf"^/v1/profiles/{_ID}/stop$"), lambda m, b: self._call("profiles_stop", m[1])),
            ("GET", re.compile(rf"^/v1/profiles/{_ID}/status$"), self._status),
            ("GET", re.compile(r"^/v1/proxies$"), lambda m, b: self._call("proxies_list")),
            ("POST", re.compile(rf"^/v1/proxies/{_ID}/check$"), lambda m, b: self._call("proxies_check", m[1])),
        ]

    # ------------------------------------------------------------------ handlers

    def _call(self, method: str, *args: Any, created: bool = False) -> Tuple[int, Any]:
        res = getattr(self.bridge, method)(*args)
        if res.get("ok"):
            return (201 if created else 200), res.get("data")
        error = str(res.get("error") or "Request failed")
        return (404 if "not found" in error.lower() else 400), {"error": error}

    def _health(self, m: Any, body: Any) -> Tuple[int, Any]:
        return 200, {"ok": True, "app": "CamouDesk", "version": __version__}

    def _start(self, m: Any, body: Any) -> Tuple[int, Any]:
        headless = bool((body or {}).get("headless", False))
        res = self.bridge.manager.launch(m[1], headless=headless)
        if res.get("ok"):
            return 200, {k: v for k, v in res.items() if k != "ok"}
        error = str(res.get("error"))
        return (404 if "does not exist" in error else 409), {"error": error}

    def _status(self, m: Any, body: Any) -> Tuple[int, Any]:
        if self.bridge.store.get_profile(m[1]) is None:
            return 404, {"error": "Profile not found"}
        return 200, self.bridge.manager.get_status(m[1])

    # ------------------------------------------------------------------ server

    def _handler(self) -> type:
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CamouDesk"
            sys_version = ""

            def log_message(self, *_args: object) -> None:
                pass

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _dispatch(self, method: str) -> None:
                host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
                if host not in ("127.0.0.1", "localhost", "::1"):
                    return self._send(403, {"error": "Forbidden host"})
                token = api._token() or ""
                auth = self.headers.get("Authorization") or ""
                supplied = auth[7:] if auth.startswith("Bearer ") else ""
                if not token or not hmac.compare_digest(supplied.encode(), token.encode()):
                    return self._send(401, {"error": "Missing or invalid API token"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "Request body too large"})
                body: Any = None
                if length:
                    try:
                        body = json.loads(self.rfile.read(length) or b"null")
                    except json.JSONDecodeError:
                        return self._send(400, {"error": "Body must be JSON"})
                    if body is not None and not isinstance(body, dict):
                        return self._send(400, {"error": "Body must be a JSON object"})
                path = self.path.split("?", 1)[0]
                allowed = False
                for verb, pattern, fn in api._routes:
                    m = pattern.match(path)
                    if not m:
                        continue
                    allowed = True
                    if verb == method:
                        try:
                            status, payload = fn(m, body)
                        except Exception as exc:  # never leak a traceback to the client
                            status, payload = 500, {"error": f"Internal error: {type(exc).__name__}"}
                        return self._send(status, payload)
                self._send(405 if allowed else 404, {"error": "Method not allowed" if allowed else "Not found"})

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._dispatch("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._dispatch("DELETE")

        return Handler

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> int:
        if self._httpd is not None:
            return self.port
        self._httpd = _Server(("127.0.0.1", self.port), self._handler())
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, name="camoudesk-api", daemon=True).start()
        return self.port

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()


def sync_with_settings(current: Optional[ApiServer], bridge: Any, settings: Dict[str, Any]) -> Optional[ApiServer]:
    """Start, restart or stop the server to match ``settings["api"]``; returns the live server."""
    api_cfg = settings.get("api") or {}
    want = bool(api_cfg.get("enabled"))
    port = int(api_cfg.get("port") or 35120)
    if current is not None and (not want or current.port != port):
        current.stop()
        current = None
    if want and current is None:
        current = ApiServer(bridge, port, lambda: bridge.settings.get()["api"]["token"])
        current.start()
    return current
