"""Loopback forwarder that authenticates to an upstream proxy on Firefox's behalf.

Firefox cannot answer SOCKS5 username/password auth and shows an interactive prompt for HTTP
proxy auth, so a direct-mode browser is pointed at this forwarder (no credentials) instead:

* http:// and https:// upstreams: Firefox speaks plain HTTP proxy protocol to the forwarder,
  which adds ``Proxy-Authorization: Basic`` to every request (TLS to the upstream for https://).
  Plain (non-CONNECT) requests get ``Connection: close`` so each carries its own credentials.
* socks5:// and socks5h:// upstreams: Firefox speaks no-auth SOCKS5 to the forwarder, which does
  the RFC 1929 handshake upstream and replays Firefox's CONNECT request unchanged, so hostnames
  are resolved by the upstream (no local DNS).

There is never a fallback to a direct connection. Credentials live only in this object's memory
and are never logged. All forwarders share one daemon thread running an asyncio event loop.
"""

import asyncio
import base64
import ssl
import threading
from typing import Callable, Optional, Set
from urllib.parse import unquote

from camoudesk.models.profile import ProxyConfig

_HEAD_LIMIT = 64 * 1024
_CONNECT_TIMEOUT = 20.0
_HANDSHAKE_TIMEOUT = 30.0
_COPY_CHUNK = 64 * 1024

ErrorCallback = Callable[[str], None]


class _LoopThread:
    _lock = threading.Lock()
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _thread: Optional[threading.Thread] = None

    @classmethod
    def get(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is None or cls._thread is None or not cls._thread.is_alive():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, name="camoudesk-proxy-forwarder", daemon=True)
                thread.start()
                cls._loop, cls._thread = loop, thread
            return cls._loop


class UpstreamError(Exception):
    """Upstream proxy failure; the message is safe to log (never contains credentials)."""


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_COPY_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _relay(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    """Copy both directions until either side closes, then close both."""
    tasks = [asyncio.ensure_future(_pipe(a_reader, b_writer)), asyncio.ensure_future(_pipe(b_reader, a_writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for w in (a_writer, b_writer):
            try:
                w.close()
            except Exception:
                pass


def _status_code(head: bytes) -> int:
    try:
        return int(head.split(b"\r\n", 1)[0].split(b" ")[1])
    except (IndexError, ValueError):
        return 0


def _rewrite_request_head(head: bytes, auth_header: bytes, force_close: bool) -> bytes:
    lines = head.rstrip(b"\r\n").split(b"\r\n")
    drop = {b"proxy-authorization"}
    if force_close:
        drop |= {b"connection", b"proxy-connection", b"keep-alive"}
    kept = [lines[0]] + [ln for ln in lines[1:] if ln.split(b":", 1)[0].strip().lower() not in drop]
    kept.append(auth_header)
    if force_close:
        kept.append(b"Connection: close")
    return b"\r\n".join(kept) + b"\r\n\r\n"


def _http_error(code: int, reason: str, detail: str) -> bytes:
    body = f"CamouDesk proxy forwarder: {detail}\n".encode()
    return (
        f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


class ProxyForwarder:
    """One loopback listener forwarding to one authenticated upstream proxy."""

    def __init__(self, upstream: ProxyConfig, port: int = 0, on_error: Optional[ErrorCallback] = None) -> None:
        if not upstream.is_configured:
            raise ValueError("Upstream proxy is not configured")
        self.upstream = upstream
        self.port = int(port or 0)
        self._on_error = on_error
        self._reported: Set[str] = set()
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: Set["asyncio.Task[None]"] = set()
        self._username = unquote(upstream.username or "")
        self._password = unquote(upstream.password or "")
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        self._auth_header = f"Proxy-Authorization: Basic {token}".encode()

    # ------------------------------------------------------------------ public API

    @property
    def is_socks(self) -> bool:
        return self.upstream.scheme in ("socks5", "socks5h")

    @property
    def local_proxy(self) -> ProxyConfig:
        """Credential-less proxy for Firefox to use, pointing at this forwarder."""
        scheme = "socks5" if self.is_socks else "http"
        return ProxyConfig(raw_url=f"{scheme}://127.0.0.1:{self.port}", scheme=scheme, host="127.0.0.1", port=self.port)

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> int:
        """Start listening (on the requested port, or a free one) and return the port."""
        loop = _LoopThread.get()
        asyncio.run_coroutine_threadsafe(self._start(), loop).result(10)
        return self.port

    def stop(self) -> None:
        if self._server is None:
            return
        loop = _LoopThread.get()
        try:
            asyncio.run_coroutine_threadsafe(self._stop(), loop).result(10)
        except Exception:
            pass

    # ------------------------------------------------------------------ lifecycle

    async def _start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task in list(self._conns):
            task.cancel()
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 5)
            except Exception:
                pass

    def _report(self, message: str) -> None:
        if self._on_error is None or message in self._reported:
            return
        self._reported.add(message)
        try:
            self._on_error(message)
        except Exception:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            if self.is_socks:
                await self._handle_socks(reader, writer)
            else:
                await self._handle_http(reader, writer)
        except UpstreamError as exc:
            self._report(str(exc))
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            if task is not None:
                self._conns.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _open_upstream(self, tls: bool) -> "tuple[asyncio.StreamReader, asyncio.StreamWriter]":
        ctx = None
        if tls:
            try:
                import certifi

                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    self.upstream.host,
                    self.upstream.port,
                    ssl=ctx,
                    server_hostname=self.upstream.host if tls else None,
                    limit=_HEAD_LIMIT,
                ),
                _CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
            raise UpstreamError(
                f"Upstream proxy {self.upstream.host}:{self.upstream.port} unreachable: {type(exc).__name__}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ HTTP / HTTPS upstream

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HANDSHAKE_TIMEOUT)
        is_connect = head[:8].upper() == b"CONNECT "
        try:
            up_reader, up_writer = await self._open_upstream(tls=self.upstream.scheme == "https")
        except UpstreamError as exc:
            writer.write(_http_error(502, "Bad Gateway", "upstream proxy unreachable"))
            await writer.drain()
            raise exc

        up_writer.write(_rewrite_request_head(head, self._auth_header, force_close=not is_connect))
        await up_writer.drain()

        if is_connect:
            reply = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), _HANDSHAKE_TIMEOUT)
            if _status_code(reply) == 407:
                up_writer.close()
                writer.write(_http_error(502, "Bad Gateway", "upstream proxy rejected the credentials"))
                await writer.drain()
                raise UpstreamError("Upstream proxy rejected the credentials (HTTP 407)")
            writer.write(reply)
            await writer.drain()
            await _relay(reader, writer, up_reader, up_writer)
            return

        # Plain request: inspect the response head so a 407 never reaches Firefox (it would
        # open an interactive credentials prompt), then stream the rest (request body included).
        body_task = asyncio.ensure_future(_pipe(reader, up_writer))
        try:
            reply = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), 120)
        except BaseException:
            body_task.cancel()
            up_writer.close()
            raise
        if _status_code(reply) == 407:
            body_task.cancel()
            up_writer.close()
            writer.write(_http_error(502, "Bad Gateway", "upstream proxy rejected the credentials"))
            await writer.drain()
            raise UpstreamError("Upstream proxy rejected the credentials (HTTP 407)")
        writer.write(reply)
        await writer.drain()
        await _pipe(up_reader, writer)
        body_task.cancel()

    # ------------------------------------------------------------------ SOCKS5 upstream

    async def _handle_socks(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Firefox -> forwarder: no-auth greeting.
        ver, nmethods = await asyncio.wait_for(reader.readexactly(2), _HANDSHAKE_TIMEOUT)
        if ver != 5:
            return
        methods = await reader.readexactly(nmethods)
        if 0x00 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")
        await writer.drain()

        # Firefox's request, kept verbatim to replay upstream (domain names are passed through).
        hdr = await reader.readexactly(4)
        _, cmd, _, atyp = hdr
        if atyp == 1:
            addr = await reader.readexactly(4)
        elif atyp == 3:
            length = await reader.readexactly(1)
            addr = length + await reader.readexactly(length[0])
        elif atyp == 4:
            addr = await reader.readexactly(16)
        else:
            writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return
        port = await reader.readexactly(2)
        if cmd != 1:
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return
        request = hdr + addr + port

        def fail(rep: int) -> None:
            writer.write(bytes([5, rep, 0, 1, 0, 0, 0, 0, 0, 0]))

        try:
            up_reader, up_writer = await self._open_upstream(tls=False)
        except UpstreamError:
            fail(0x01)
            await writer.drain()
            raise

        try:
            up_writer.write(b"\x05\x02\x00\x02")  # offer no-auth and username/password
            await up_writer.drain()
            sver, method = await asyncio.wait_for(up_reader.readexactly(2), _HANDSHAKE_TIMEOUT)
            if sver != 5 or method == 0xFF:
                raise UpstreamError("SOCKS5 upstream refused all authentication methods")
            if method == 0x02:
                user, pwd = self._username.encode(), self._password.encode()
                if len(user) > 255 or len(pwd) > 255:
                    raise UpstreamError("SOCKS5 username or password longer than 255 bytes")
                up_writer.write(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
                await up_writer.drain()
                _, status = await asyncio.wait_for(up_reader.readexactly(2), _HANDSHAKE_TIMEOUT)
                if status != 0:
                    raise UpstreamError("SOCKS5 upstream rejected the credentials")
            elif method != 0x00:
                raise UpstreamError(f"SOCKS5 upstream chose unsupported auth method {method}")

            up_writer.write(request)
            await up_writer.drain()
            rhdr = await asyncio.wait_for(up_reader.readexactly(4), _HANDSHAKE_TIMEOUT)
            ratyp = rhdr[3]
            if ratyp == 1:
                rest = await up_reader.readexactly(4 + 2)
            elif ratyp == 3:
                ln = await up_reader.readexactly(1)
                rest = ln + await up_reader.readexactly(ln[0] + 2)
            elif ratyp == 4:
                rest = await up_reader.readexactly(16 + 2)
            else:
                raise UpstreamError("SOCKS5 upstream sent a malformed reply")
        except UpstreamError:
            fail(0x02)
            await writer.drain()
            up_writer.close()
            raise
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as exc:
            fail(0x01)
            await writer.drain()
            up_writer.close()
            raise UpstreamError(f"SOCKS5 upstream handshake failed: {type(exc).__name__}") from exc

        writer.write(rhdr + rest)
        await writer.drain()
        if rhdr[1] != 0:
            up_writer.close()
            return
        await _relay(reader, writer, up_reader, up_writer)
