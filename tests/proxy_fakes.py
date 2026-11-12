"""Loopback test doubles: an origin web server and authenticating HTTP and SOCKS5 upstream proxies.

Both proxies route every requested hostname to the local origin, so tests (and a real browser)
can use names like ``origin.camoudesk.test`` that only resolve "through the proxy".
"""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import socketserver
import threading
from typing import List, Optional, Tuple


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(a: socket.socket, b: socket.socket) -> None:
    t = threading.Thread(target=_pump, args=(b, a), daemon=True)
    t.start()
    _pump(a, b)
    t.join(5)


def recv_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    buf = b""
    while marker not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def recv_all(sock: socket.socket) -> bytes:
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return buf
        buf += chunk


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


class OriginServer:
    """Answers every GET with ``hello <path>`` and records request paths and Cookie headers."""

    def __init__(self) -> None:
        self.paths: List[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                outer.paths.append(self.path)
                body = f"hello {self.path}".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeHttpProxy:
    """HTTP proxy requiring Basic auth; supports CONNECT and absolute-URI requests."""

    def __init__(self, origin_port: int, username: str, password: str) -> None:
        self.expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.auth_headers: List[Optional[str]] = []
        self.request_lines: List[str] = []
        self.connection_headers: List[str] = []
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                outer._handle(self.request, origin_port)

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, client: socket.socket, origin_port: int) -> None:
        head = recv_until(client, b"\r\n\r\n")
        if not head:
            return
        head_part, _, rest = head.partition(b"\r\n\r\n")
        lines = head_part.decode("latin-1").split("\r\n")
        self.request_lines.append(lines[0])
        headers = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
        self.auth_headers.append(headers.get("proxy-authorization"))
        self.connection_headers.append(headers.get("connection", ""))
        if headers.get("proxy-authorization") != self.expected:
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="fake"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            )
            client.close()
            return
        method, target, version = lines[0].split(" ", 2)
        upstream = socket.create_connection(("127.0.0.1", origin_port))
        if method.upper() == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            path = "/" + target.split("://", 1)[-1].split("/", 1)[-1] if "://" in target else target
            kept = [ln for ln in lines[1:] if not ln.lower().startswith(("proxy-authorization", "proxy-connection"))]
            upstream.sendall(f"{method} {path} {version}\r\n".encode() + "\r\n".join(kept).encode() + b"\r\n\r\n" + rest)
        relay(client, upstream)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeSocks5Proxy:
    """SOCKS5 proxy requiring RFC 1929 username/password; records the requested destinations."""

    def __init__(self, origin_port: int, username: str, password: str) -> None:
        self.creds = (username.encode(), password.encode())
        self.destinations: List[Tuple[int, str, int]] = []
        self.auth_ok: List[bool] = []
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    outer._handle(self.request, origin_port)
                except (ConnectionError, OSError):
                    pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, client: socket.socket, origin_port: int) -> None:
        ver, n = recv_exact(client, 2)
        methods = recv_exact(client, n)
        if ver != 5 or 2 not in methods:
            client.sendall(b"\x05\xff")
            return
        client.sendall(b"\x05\x02")
        _, ulen = recv_exact(client, 2)
        user = recv_exact(client, ulen)
        plen = recv_exact(client, 1)[0]
        pwd = recv_exact(client, plen)
        ok = (user, pwd) == self.creds
        self.auth_ok.append(ok)
        client.sendall(b"\x01\x00" if ok else b"\x01\x01")
        if not ok:
            return
        _, cmd, _, atyp = recv_exact(client, 4)
        if atyp == 3:
            host = recv_exact(client, recv_exact(client, 1)[0]).decode()
        elif atyp == 1:
            host = socket.inet_ntoa(recv_exact(client, 4))
        else:
            host = socket.inet_ntop(socket.AF_INET6, recv_exact(client, 16))
        port = int.from_bytes(recv_exact(client, 2), "big")
        self.destinations.append((atyp, host, port))
        upstream = socket.create_connection(("127.0.0.1", origin_port))
        client.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + origin_port.to_bytes(2, "big"))
        relay(client, upstream)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    """A loopback port with nothing listening on it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
