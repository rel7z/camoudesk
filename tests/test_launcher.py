"""Tests for the direct Camoufox launcher: proxy prefs, mode selection, config parity, cookies."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pytest

from camoudesk.core.fingerprint import ensure_profile_fingerprint
from camoudesk.core.launcher import build_camoufox_kwargs, build_launch_spec, has_proxy_credentials
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import MODE_DIRECT, MODE_WORKER, ProcessManager
from camoudesk.models.profile import Profile, ProxyConfig
from proxy_fakes import FakeHttpProxy, FakeSocks5Proxy, OriginServer


def _decode_chunked(env: dict, prefix: str) -> dict:
    keys = sorted((k for k in env if k.startswith(prefix)), key=lambda k: int(k.rsplit("_", 1)[1]))
    return json.loads("".join(env[k] for k in keys)) if keys else {}


class TestProxyPrefs(unittest.TestCase):
    def test_http_proxy_prefs(self) -> None:
        prefs = ProxyConfig.from_url("http://10.0.0.5:3128").to_firefox_prefs()
        self.assertEqual(prefs["network.proxy.type"], 1)
        self.assertEqual((prefs["network.proxy.http"], prefs["network.proxy.http_port"]), ("10.0.0.5", 3128))
        self.assertEqual((prefs["network.proxy.ssl"], prefs["network.proxy.ssl_port"]), ("10.0.0.5", 3128))
        self.assertFalse(prefs["network.proxy.failover_direct"])

    def test_socks_proxy_always_resolves_dns_remotely(self) -> None:
        for url in ("socks5://10.0.0.5:1080", "socks5h://10.0.0.5:1080"):
            with self.subTest(url=url):
                prefs = ProxyConfig.from_url(url).to_firefox_prefs()
                self.assertEqual(prefs["network.proxy.socks_version"], 5)
                self.assertTrue(prefs["network.proxy.socks_remote_dns"])
                self.assertFalse(prefs["network.proxy.failover_direct"])

    def test_https_proxy_uses_pac(self) -> None:
        prefs = ProxyConfig.from_url("https://proxy.example:8443").to_firefox_prefs()
        self.assertEqual(prefs["network.proxy.type"], 2)
        self.assertIn("HTTPS proxy.example:8443", prefs["network.proxy.autoconfig_url"])

    def test_credentials_never_reach_firefox_prefs(self) -> None:
        with self.assertRaises(ValueError):
            ProxyConfig.from_url("socks5://u:p@10.0.0.5:1080").to_firefox_prefs()
        self.assertTrue(has_proxy_credentials(Profile(id="a", name="a", proxy="http://u:p@10.0.0.5:1")))
        self.assertFalse(has_proxy_credentials(Profile(id="b", name="b", proxy="http://10.0.0.5:1")))
        self.assertFalse(has_proxy_credentials(Profile(id="c", name="c")))
        self.assertEqual(ProxyConfig().to_firefox_prefs(), {})


class TestEnvAndDefaults(unittest.TestCase):
    def test_browser_env_keeps_camoufox_vars_and_drops_secrets(self) -> None:
        from camoudesk.core.launcher import _browser_env

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-secret", "HOME": "/Users/t", "HTTPS_PROXY": "http://127.0.0.1:9"}):
            launch_env = {**os.environ, "CAMOU_CONFIG_1": "{}", "CAMOU_PREFS_1": "{}"}
            env = _browser_env(launch_env)
        self.assertEqual((env["CAMOU_CONFIG_1"], env["CAMOU_PREFS_1"], env["HOME"]), ("{}", "{}", "/Users/t"))
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_auto_timezone_and_locale_are_not_spoofed(self) -> None:
        kw = build_camoufox_kwargs("macos", "/tmp/ff", {}, timezone="auto", locale="auto")
        self.assertNotIn("config", kw)
        self.assertNotIn("locale", kw)
        kw = build_camoufox_kwargs("macos", "/tmp/ff", {}, timezone="Europe/Berlin", locale="de-DE")
        self.assertEqual((kw["config"], kw["locale"]), ({"timezone": "Europe/Berlin"}, "de-DE"))

    def test_socks4(self) -> None:
        cfg = ProxyConfig.from_url("socks4://10.0.0.5:1080")
        self.assertEqual(cfg.to_firefox_prefs()["network.proxy.socks_version"], 4)
        with self.assertRaises(ValueError):
            ProxyConfig.from_url("socks4://u:p@10.0.0.5:1080")

    def test_new_profiles_default_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(app_dir=Path(tmp) / "app", legacy_dir=Path(tmp) / "legacy")
            starter = store.list_profiles()[0]
            self.assertEqual((starter.name, starter.timezone, starter.locale), ("Profile 1", "auto", "auto"))
            created = store.create_profile({"name": "New"})
            self.assertEqual((created.timezone, created.locale), ("auto", "auto"))


class TestModeSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.manager = ProcessManager(store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_modes(self) -> None:
        plain = Profile(id="p", name="p")
        authed = Profile(id="q", name="q", proxy="socks5://u:p@10.0.0.1:1080")
        self.assertEqual(self.manager.default_mode(plain, headless=False), MODE_DIRECT)
        self.assertEqual(self.manager.default_mode(plain, headless=True), MODE_WORKER)
        self.assertEqual(self.manager.default_mode(authed, headless=False), MODE_DIRECT)

    def test_launch_spec_requires_forwarder_for_credentials(self) -> None:
        prof = self.store.create_profile({"name": "Authed", "proxy": "http://u:p@10.0.0.1:8080"})
        with self.assertRaises(ValueError):
            build_launch_spec(prof, self.store)
        with self.assertRaises(ValueError):
            build_launch_spec(prof, self.store, local_proxy=ProxyConfig.from_url("http://u:p@127.0.0.1:1"))


@pytest.mark.live_browser
class TestDirectLaunchLive(unittest.TestCase):
    """Requires the Camoufox browser binary (`camoufox fetch`)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_direct_and_worker_compute_identical_identity(self) -> None:
        from camoufox.utils import launch_options

        prof = self.store.create_profile(
            {"name": "Parity", "os": "windows", "timezone": "Europe/Berlin", "locale": "de-DE", "block_webrtc": False}
        )
        spec = build_launch_spec(prof, self.store)

        fp, _ = ensure_profile_fingerprint(self.store.profile_dir(prof.id), prof.os)
        kwargs = build_camoufox_kwargs(
            prof.os,
            str(self.store.firefox_data_dir(prof.id)),
            fp,
            timezone=prof.timezone,
            locale=prof.locale,
            block_webrtc=prof.block_webrtc,
            humanize=prof.humanize,
        )
        kwargs.pop("persistent_context")
        # Exactly what camoufox.sync_api.Camoufox(persistent_context=True, **kwargs) computes.
        worker_opts = launch_options(pin_cpu_cores=False, **kwargs)

        self.assertEqual(_decode_chunked(spec.env, "CAMOU_CONFIG_"), _decode_chunked(worker_opts["env"], "CAMOU_CONFIG_"))
        self.assertEqual(_decode_chunked(spec.env, "CAMOU_PREFS_"), _decode_chunked(worker_opts["env"], "CAMOU_PREFS_"))
        self.assertIn("-no-remote", spec.args)
        self.assertEqual(spec.args[spec.args.index("-profile") + 1], str(self.store.firefox_data_dir(prof.id)))

    def test_direct_mode_through_authenticating_proxies(self) -> None:
        """A real browser reaches a host that only resolves via the proxy, through the forwarder."""
        for scheme, fake_cls in (("http", FakeHttpProxy), ("socks5", FakeSocks5Proxy)):
            with self.subTest(scheme=scheme):
                origin = OriginServer()
                upstream = fake_cls(origin.port, "live-user", "live-pw")
                manager = ProcessManager(store=self.store)
                try:
                    prof = self.store.create_profile(
                        {"name": f"Authed {scheme}", "proxy": f"{scheme}://live-user:live-pw@127.0.0.1:{upstream.port}"}
                    )
                    path = f"/live-{scheme}"
                    res = manager.launch(
                        prof.id, headless=True, mode=MODE_DIRECT, url_override=f"http://origin.camoudesk.test{path}"
                    )
                    self.assertTrue(res["ok"], res)
                    deadline = time.monotonic() + 60
                    while path not in origin.paths and time.monotonic() < deadline:
                        time.sleep(0.1)
                    self.assertIn(path, origin.paths, manager.read_logs(prof.id, 40))
                    if scheme == "http":
                        self.assertIn(upstream.expected, upstream.auth_headers)
                    else:
                        self.assertTrue(upstream.auth_ok and all(upstream.auth_ok))
                        self.assertIn((3, "origin.camoudesk.test", 80), upstream.destinations)
                    self.assertNotIn("live-pw", manager.read_logs(prof.id, 500))
                    self.assertTrue(manager.stop(prof.id)["exited"])
                finally:
                    manager.stop_all()
                    upstream.close()
                    origin.close()

    def test_direct_mode_persists_cookies_across_graceful_stop(self) -> None:
        observed: list = []
        hits = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                if self.path.startswith("/set"):
                    self.send_header("Set-Cookie", "camoudesk_id=direct_ok; Path=/; Max-Age=3600")
                if self.path.startswith("/check"):
                    observed.append(self.headers.get("Cookie", ""))
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                hits.set()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        manager = ProcessManager(store=self.store)
        try:
            prof = self.store.create_profile({"name": "Direct cookies"})
            for path in ("/set", "/check"):
                hits.clear()
                res = manager.launch(prof.id, headless=True, mode=MODE_DIRECT, url_override=f"http://127.0.0.1:{port}{path}")
                self.assertTrue(res["ok"], res)
                self.assertTrue(self.store.run_file(prof.id).is_file())
                self.assertTrue(hits.wait(60), manager.read_logs(prof.id, 30))
                time.sleep(0.5)
                stopped = manager.stop(prof.id)
                self.assertTrue(stopped["exited"], stopped)
                self.assertFalse(self.store.run_file(prof.id).exists())
            self.assertTrue(any("camoudesk_id=direct_ok" in c for c in observed), observed)
        finally:
            manager.stop_all()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
