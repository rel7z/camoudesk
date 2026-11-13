"""Unit tests for proxy parsing, credential masking, and health probes."""

import unittest
from unittest.mock import MagicMock

from camoudesk.core.proxy import check_proxy_health
from camoudesk.models.profile import ProxyConfig


class TestProxySupport(unittest.TestCase):
    def test_proxy_parsing_and_masking(self) -> None:
        cfg = ProxyConfig.from_url("http://alice:supersecret@127.0.0.1:8080")
        self.assertEqual(cfg.scheme, "http")
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.username, "alice")
        self.assertEqual(cfg.password, "supersecret")
        self.assertNotIn("supersecret", cfg.masked_display())
        self.assertEqual(cfg.masked_display(), "http://alice:***@127.0.0.1:8080")

        pw = cfg.to_playwright_proxy()
        self.assertEqual(
            pw,
            {"server": "http://127.0.0.1:8080", "username": "alice", "password": "supersecret"},
        )

    def test_socks5_and_validation_errors(self) -> None:
        cfg = ProxyConfig.from_url("socks5://127.0.0.1:1080")
        self.assertEqual(cfg.scheme, "socks5")
        self.assertEqual(cfg.port, 1080)

        with self.assertRaises(ValueError):
            ProxyConfig.from_url("ftp://127.0.0.1:21")
        with self.assertRaises(ValueError):
            ProxyConfig.from_url("http://127.0.0.1")

    def test_check_proxy_health_mocked(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "query": "203.0.113.10",
            "country": "United States",
            "city": "New York",
            "timezone": "America/New_York",
        }
        mock_get = MagicMock(return_value=mock_resp)

        res = check_proxy_health("http://u:p@127.0.0.1:8080", http_get=mock_get)
        self.assertTrue(res["ok"])
        self.assertEqual(res["ip"], "203.0.113.10")
        self.assertEqual(res["timezone"], "America/New_York")
        mock_get.assert_called_once()

    def test_check_proxy_health_failure(self) -> None:
        failing_get = MagicMock(side_effect=ConnectionError("refused"))
        res = check_proxy_health("http://127.0.0.1:9999", http_get=failing_get)
        self.assertFalse(res["ok"])
        self.assertIn("refused", res["error"])

    def test_failures_get_actionable_messages_without_credentials(self) -> None:
        import requests

        cases = [
            (requests.exceptions.ProxyError("HTTPSConnectionPool(host='ipinfo.io', port=443): Max retries exceeded "
                                            "(Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection "
                                            "failed: 407 Proxy Authentication Required')))"), "username or password (HTTP 407)"),
            (requests.exceptions.ConnectTimeout("Connection to 203.0.113.9 timed out"), "did not answer in time"),
            (ConnectionError("[Errno 61] Connection refused"), "refused the connection"),
            (requests.exceptions.ConnectionError("Failed to resolve 'nope.invalid' (nodename nor servname provided)"), "could not be found"),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                res = check_proxy_health("http://acct:s3cret@proxy.example:8000", http_get=MagicMock(side_effect=exc))
                self.assertFalse(res["ok"])
                self.assertIn(expected, res["error"])
                self.assertNotIn("s3cret", str(res))

    def test_success_does_not_echo_the_credentialed_url(self) -> None:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ip": "203.0.113.10", "country": "US", "timezone": "UTC"}
        res = check_proxy_health("http://acct:s3cret@proxy.example:8000", http_get=MagicMock(return_value=resp))
        self.assertTrue(res["ok"])
        self.assertNotIn("s3cret", str(res))


if __name__ == "__main__":
    unittest.main()
