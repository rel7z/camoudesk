"""Unit tests for the loopback authenticating ProxyForwarder against fake upstream proxies."""

import socket
from typing import List
import unittest
import urllib.error
import urllib.request

from camoudesk.core.proxy_forwarder import ProxyForwarder
from camoudesk.models.profile import ProxyConfig
from proxy_fakes import FakeHttpProxy, FakeSocks5Proxy, OriginServer, closed_port, recv_all, recv_exact, recv_until

USER, PASSWORD = "alice", "s3cret-pw"


def _http_get_via(proxy_port: int, url: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"}))
    with opener.open(url, timeout=10) as resp:
        return resp.read()


def _socks_connect(port: int, host: str, dport: int) -> "tuple[socket.socket, int]":
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"\x05\x01\x00")
    assert recv_exact(s, 2) == b"\x05\x00"
    name = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + dport.to_bytes(2, "big"))
    reply = recv_exact(s, 10)
    return s, reply[1]


class ForwarderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.origin = OriginServer()
        self.errors: List[str] = []
        self.forwarders: List[ProxyForwarder] = []

    def tearDown(self) -> None:
        for f in self.forwarders:
            f.stop()
        self.origin.close()

    def forwarder(self, url: str, port: int = 0) -> ProxyForwarder:
        f = ProxyForwarder(ProxyConfig.from_url(url), port=port, on_error=self.errors.append)
        f.start()
        self.forwarders.append(f)
        return f

    def assert_no_secret_logged(self) -> None:
        for msg in self.errors:
            self.assertNotIn(PASSWORD, msg)


class TestHttpUpstream(ForwarderTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.upstream = FakeHttpProxy(self.origin.port, USER, PASSWORD)

    def tearDown(self) -> None:
        self.upstream.close()
        super().tearDown()

    def test_plain_request_gets_auth_and_connection_close(self) -> None:
        fwd = self.forwarder(f"http://{USER}:{PASSWORD}@127.0.0.1:{self.upstream.port}")
        self.assertEqual(fwd.local_proxy.raw_url, f"http://127.0.0.1:{fwd.port}")
        self.assertFalse(fwd.local_proxy.has_credentials)

        self.assertEqual(_http_get_via(fwd.port, "http://origin.camoudesk.test/plain"), b"hello /plain")
        self.assertEqual(self.upstream.auth_headers, [self.upstream.expected])
        self.assertEqual(self.upstream.connection_headers[-1].lower(), "close")
        self.assertIn("origin.camoudesk.test", self.upstream.request_lines[-1])

    def test_connect_tunnel(self) -> None:
        fwd = self.forwarder(f"http://{USER}:{PASSWORD}@127.0.0.1:{self.upstream.port}")
        s = socket.create_connection(("127.0.0.1", fwd.port), timeout=10)
        s.sendall(b"CONNECT origin.camoudesk.test:443 HTTP/1.1\r\nHost: origin.camoudesk.test:443\r\n\r\n")
        self.assertIn(b" 200 ", recv_until(s, b"\r\n\r\n"))
        s.sendall(b"GET /tunnel HTTP/1.1\r\nHost: origin.camoudesk.test\r\nConnection: close\r\n\r\n")
        self.assertIn(b"hello /tunnel", recv_all(s))
        s.close()
        self.assertEqual(self.upstream.auth_headers, [self.upstream.expected])
        self.assertTrue(self.upstream.request_lines[0].startswith("CONNECT origin.camoudesk.test:443"))

    def test_rejected_credentials_become_502_not_a_browser_prompt(self) -> None:
        fwd = self.forwarder(f"http://{USER}:wrong-{PASSWORD}@127.0.0.1:{self.upstream.port}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _http_get_via(fwd.port, "http://origin.camoudesk.test/x")
        self.assertEqual(ctx.exception.code, 502)

        s = socket.create_connection(("127.0.0.1", fwd.port), timeout=10)
        s.sendall(b"CONNECT origin.camoudesk.test:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b" 502 ", recv_until(s, b"\r\n\r\n"))
        s.close()
        self.assertEqual(self.errors, ["Upstream proxy rejected the credentials (HTTP 407)"])
        self.assert_no_secret_logged()

    def test_percent_encoded_credentials_are_decoded(self) -> None:
        self.upstream.close()
        self.upstream = FakeHttpProxy(self.origin.port, "user@corp", "p:w/d")
        fwd = self.forwarder(f"http://user%40corp:p%3Aw%2Fd@127.0.0.1:{self.upstream.port}")
        self.assertEqual(_http_get_via(fwd.port, "http://origin.camoudesk.test/enc"), b"hello /enc")

    def test_unreachable_upstream_returns_502_and_logs(self) -> None:
        fwd = self.forwarder(f"http://{USER}:{PASSWORD}@127.0.0.1:{closed_port()}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _http_get_via(fwd.port, "http://origin.camoudesk.test/x")
        self.assertEqual(ctx.exception.code, 502)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("unreachable", self.errors[0])
        self.assert_no_secret_logged()


class TestSocksUpstream(ForwarderTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.upstream = FakeSocks5Proxy(self.origin.port, USER, PASSWORD)

    def tearDown(self) -> None:
        self.upstream.close()
        super().tearDown()

    def test_auth_handshake_and_remote_dns(self) -> None:
        for scheme in ("socks5", "socks5h"):
            with self.subTest(scheme=scheme):
                fwd = self.forwarder(f"{scheme}://{USER}:{PASSWORD}@127.0.0.1:{self.upstream.port}")
                self.assertEqual(fwd.local_proxy.scheme, "socks5")
                s, rep = _socks_connect(fwd.port, "origin.camoudesk.test", 80)
                self.assertEqual(rep, 0)
                s.sendall(b"GET /socks HTTP/1.1\r\nHost: origin.camoudesk.test\r\nConnection: close\r\n\r\n")
                self.assertIn(b"hello /socks", recv_all(s))
                s.close()
                # The hostname reached the upstream unresolved (ATYP 3 = domain name).
                self.assertEqual(self.upstream.destinations[-1], (3, "origin.camoudesk.test", 80))
                self.assertTrue(self.upstream.auth_ok[-1])

    def test_rejected_credentials(self) -> None:
        fwd = self.forwarder(f"socks5://{USER}:nope@127.0.0.1:{self.upstream.port}")
        s, rep = _socks_connect(fwd.port, "origin.camoudesk.test", 80)
        s.close()
        self.assertNotEqual(rep, 0)
        self.assertEqual(self.upstream.destinations, [])
        self.assertEqual(self.errors, ["SOCKS5 upstream rejected the credentials"])

    def test_unreachable_upstream(self) -> None:
        fwd = self.forwarder(f"socks5://{USER}:{PASSWORD}@127.0.0.1:{closed_port()}")
        s, rep = _socks_connect(fwd.port, "origin.camoudesk.test", 80)
        s.close()
        self.assertEqual(rep, 1)
        self.assertIn("unreachable", self.errors[0])
        self.assert_no_secret_logged()


class TestLifecycle(ForwarderTestBase):
    def test_stop_closes_listener_and_port_can_be_reused(self) -> None:
        upstream = FakeHttpProxy(self.origin.port, USER, PASSWORD)
        try:
            url = f"http://{USER}:{PASSWORD}@127.0.0.1:{upstream.port}"
            first = self.forwarder(url)
            port = first.port
            first.stop()
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
            # A restarted GUI re-binds the port recorded in run.json.
            second = self.forwarder(url, port=port)
            self.assertEqual(second.port, port)
            self.assertEqual(_http_get_via(port, "http://origin.camoudesk.test/again"), b"hello /again")
        finally:
            upstream.close()


if __name__ == "__main__":
    unittest.main()
