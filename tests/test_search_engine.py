"""Tests for the address-bar search engine written into profiles (core/search_engine.py)."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camoudesk.core import search_engine as se
from camoudesk.core.launcher import profile_camoufox_kwargs, resolve_network
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore

# search.json.mozlz4 exactly as Camoufox 158 wrote it for a fresh profile (has LZ4 back-references).
FIREFOX_FILE = bytes.fromhex(
    "6d6f7a4c7a343000d9000000f3217b2276657273696f6e223a31342c22656e67696e6573223a5b7b226964223a226e6f"
    "6e65222c225f6e616d65223a224e0f00916973436f6e666967453000f70a223a747275652c225f6d6574614461746122"
    "3a7b7d7d5d2c22100060226c6f63616c4400816964222c227265677500f22822756e6b6e6f776e222c226368616e6e65"
    "6c223a2264656661756c74222c226578706572696d656e74223a22222c2264697374726f49440e0042617070442b0002"
    "8800c04964223a226e6f6e65227d7d"
)
POLICIES = {
    "policies": {
        "SearchEngines": {
            "PreventInstalls": True,
            "Remove": ["Google", "DuckDuckGo", "Bing", "Amazon.com", "eBay", "Twitter", "Wikipedia (en)"],
            "Default": "None",
            "Add": [{"Name": "None", "URLTemplate": "http://127.0.0.1"}],
        }
    }
}


class TestMozLz4(unittest.TestCase):
    def test_reads_a_file_firefox_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / se.SETTINGS_FILE
            path.write_bytes(FIREFOX_FILE)
            data = se.read_mozlz4(path)
        self.assertEqual(data["version"], 14)
        self.assertEqual(data["engines"], [{"id": "none", "_name": "None", "_isConfigEngine": True, "_metaData": {}}])
        self.assertEqual(data["metaData"]["appDefaultEngineId"], "none")

    def test_round_trip_across_literal_length_boundaries(self) -> None:
        for n in (0, 1, 14, 15, 16, 269, 270, 271, 5000):
            payload = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
            block = se.lz4_block_store(payload)
            self.assertEqual(se.lz4_block_decompress(block, len(payload)), payload, n)

    def test_matches_the_reference_lz4_library_when_installed(self) -> None:
        lz4_block = __import__("pytest").importorskip("lz4.block")
        payload = json.dumps({"engines": [se.engine_record(k) for k in se.ENGINES]}).encode()
        self.assertEqual(lz4_block.decompress(se.lz4_block_store(payload), uncompressed_size=len(payload)), payload)
        compressed = lz4_block.compress(payload, store_size=False)
        self.assertEqual(se.lz4_block_decompress(compressed, len(payload)), payload)

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            se.lz4_block_decompress(b"\x1f\x41\x09\x00", 20)  # offset beyond output


class TestApplySearchEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ff = Path(self.tmp.name) / "firefox_data"
        self.ff.mkdir()
        self.path = self.ff / se.SETTINGS_FILE

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_verification_hash_follows_firefox_salt(self) -> None:
        import base64, hashlib

        salt = "firefox_data" + "camoudesk-google" + se._DISCLAIMER.replace("$appName", "Camoufox")
        expected = base64.b64encode(hashlib.sha256(salt.encode()).digest()).decode()
        self.assertEqual(se.verification_hash("camoudesk-google", "firefox_data", "Camoufox"), expected)
        self.assertNotEqual(expected, se.verification_hash("camoudesk-google", "other", "Camoufox"))

    def test_existing_profile_keeps_firefox_engines_and_metadata(self) -> None:
        self.path.write_bytes(FIREFOX_FILE)
        self.assertTrue(se.apply_search_engine(self.ff, "duckduckgo"))
        data = se.read_mozlz4(self.path)
        self.assertEqual([e["id"] for e in data["engines"]], ["none", "camoudesk-duckduckgo"])
        ddg = data["engines"][1]
        self.assertEqual(ddg["_loadPath"], "[user]")
        self.assertEqual(ddg["_urls"][0]["template"], "https://duckduckgo.com/?q={searchTerms}")
        meta = data["metaData"]
        self.assertEqual(meta["defaultEngineId"], "camoudesk-duckduckgo")
        self.assertEqual(meta["defaultEngineIdHash"], se.verification_hash("camoudesk-duckduckgo", "firefox_data", "Camoufox"))
        self.assertEqual(meta["appDefaultEngineId"], "none")
        self.assertEqual(data["version"], 14)

    def test_unchanged_choice_does_not_rewrite(self) -> None:
        self.path.write_bytes(FIREFOX_FILE)
        se.apply_search_engine(self.ff, "google")
        before = self.path.read_bytes()
        self.assertFalse(se.apply_search_engine(self.ff, "google"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_switching_replaces_our_engine_only(self) -> None:
        self.path.write_bytes(FIREFOX_FILE)
        se.apply_search_engine(self.ff, "google")
        data = se.read_mozlz4(self.path)
        data["engines"].append({"id": "u1", "_name": "My wiki", "_loadPath": "[user]", "_urls": [{"template": "https://w/?q={searchTerms}"}]})
        self.path.write_bytes(se.mozlz4_bytes(data))
        se.apply_search_engine(self.ff, "brave")
        ids = [e["id"] for e in se.read_mozlz4(self.path)["engines"]]
        self.assertEqual(ids, ["none", "u1", "camoudesk-brave"])

    def test_none_restores_camoufox_default(self) -> None:
        self.path.write_bytes(FIREFOX_FILE)
        se.apply_search_engine(self.ff, "bing")
        self.assertTrue(se.apply_search_engine(self.ff, se.NONE))
        data = se.read_mozlz4(self.path)
        self.assertEqual([e["id"] for e in data["engines"]], ["none"])
        self.assertEqual(data["metaData"]["defaultEngineId"], "")
        self.assertFalse(se.apply_search_engine(self.ff, se.NONE))

    def test_none_keeps_a_default_the_user_chose(self) -> None:
        data = se.read_mozlz4(self._write(FIREFOX_FILE))
        data["metaData"].update(defaultEngineId="u1", defaultEngineIdHash="h")
        self.path.write_bytes(se.mozlz4_bytes(data))
        self.assertFalse(se.apply_search_engine(self.ff, se.NONE))

    def test_new_profile_gets_a_minimal_file(self) -> None:
        self.assertTrue(se.apply_search_engine(self.ff, "startpage", app_name="Camoufox"))
        data = se.read_mozlz4(self.path)
        self.assertEqual(data["version"], se.SETTINGS_VERSION)
        self.assertEqual([e["id"] for e in data["engines"]], ["camoudesk-startpage"])
        self.assertEqual(data["metaData"]["defaultEngineId"], "camoudesk-startpage")

    def test_new_profile_with_none_writes_nothing(self) -> None:
        self.assertFalse(se.apply_search_engine(self.ff, se.NONE))
        self.assertFalse(self.path.exists())

    def test_corrupt_file_is_left_alone(self) -> None:
        self.path.write_bytes(b"mozLz40\0garbage!")
        with self.assertRaises(ValueError):
            se.apply_search_engine(self.ff, "google")
        self.assertEqual(self.path.read_bytes(), b"mozLz40\0garbage!")
        with patch.object(se, "camoufox_resources_dir", return_value=None):
            self.assertEqual(se.prepare_profile_search(self.ff, "google"), {})

    def test_unknown_choice_rejected(self) -> None:
        with self.assertRaises(ValueError):
            se.apply_search_engine(self.ff, "yahoo")

    def _write(self, data: bytes) -> Path:
        self.path.write_bytes(data)
        return self.path


class TestCamoufoxInstall(unittest.TestCase):
    def test_policy_prefs_match_firefox_run_once_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp)
            (res / "distribution").mkdir()
            (res / "distribution" / "policies.json").write_text(json.dumps(POLICIES))
            (res / "application.ini").write_text("[App]\nVendor=Camoufox\nName=Camoufox\n")
            prefs = se.policy_prefs(res)
            self.assertEqual(se.camoufox_app_name(res), "Camoufox")
        self.assertEqual(prefs, {
            "browser.policies.runOncePerModification.setDefaultSearchEngine": "None",
            # JavaScript's JSON.stringify output: no spaces.
            "browser.policies.runOncePerModification.removeSearchEngines":
                '["Google","DuckDuckGo","Bing","Amazon.com","eBay","Twitter","Wikipedia (en)"]',
        })

    def test_engine_names_stay_off_camoufox_remove_list(self) -> None:
        removed = set(POLICIES["policies"]["SearchEngines"]["Remove"]) | {"None"}
        self.assertFalse({name for name, _ in se.ENGINES.values()} & removed)

    def test_missing_install_is_harmless(self) -> None:
        self.assertEqual(se.policy_prefs(None), {})
        self.assertEqual(se.camoufox_app_name(None), "Camoufox")


class TestLaunchIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")
        self.res = root / "res"
        (self.res / "distribution").mkdir(parents=True)
        (self.res / "distribution" / "policies.json").write_text(json.dumps(POLICIES))
        patcher = patch.object(se, "camoufox_resources_dir", return_value=self.res)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _kwargs(self):
        prof = self.store.create_profile({"name": "Search", "os": "macos"})
        kwargs, _ = profile_camoufox_kwargs(prof, self.store, resolve_network(prof), headless=True)
        return prof, kwargs

    def test_default_setting_installs_duckduckgo_and_presets_policy(self) -> None:
        prof, kwargs = self._kwargs()
        data = se.read_mozlz4(self.store.firefox_data_dir(prof.id) / se.SETTINGS_FILE)
        self.assertEqual(data["metaData"]["defaultEngineId"], "camoudesk-duckduckgo")
        prefs = kwargs["firefox_user_prefs"]
        self.assertEqual(prefs["browser.policies.runOncePerModification.setDefaultSearchEngine"], "None")

    def test_none_setting_adds_no_prefs_or_file(self) -> None:
        SettingsStore(self.store.app_dir).update({"search_engine": "none"})
        prof, kwargs = self._kwargs()
        self.assertNotIn("firefox_user_prefs", kwargs)
        self.assertFalse((self.store.firefox_data_dir(prof.id) / se.SETTINGS_FILE).exists())

    def test_settings_validate_choice(self) -> None:
        settings = SettingsStore(self.store.app_dir)
        self.assertEqual(settings.get()["search_engine"], "duckduckgo")
        with self.assertRaises(ValueError):
            settings.update({"search_engine": "yahoo"})
        self.assertEqual(settings.update({"search_engine": "brave"})["search_engine"], "brave")


# ---------------------------------------------------------------- live: ask Firefox itself


def _marionette(port: int, script: str, timeout: float = 60.0):
    """Run an async chrome-context script through Marionette and return its result."""
    import socket
    import time

    deadline = time.time() + timeout
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.3)
    sock.settimeout(timeout)
    buf = b""

    def recv():
        nonlocal buf
        while b":" not in buf:
            buf += sock.recv(65536)
        size, _, rest = buf.partition(b":")
        while len(rest) < int(size):
            rest += sock.recv(65536)
        buf = rest[int(size):]
        return json.loads(rest[: int(size)])

    def send(msg_id, name, params):
        data = json.dumps([0, msg_id, name, params]).encode()
        sock.sendall(str(len(data)).encode() + b":" + data)
        reply = recv()
        if reply[2]:
            raise RuntimeError(reply[2])
        return reply[3]

    with sock:
        recv()  # handshake
        send(1, "WebDriver:NewSession", {})
        send(2, "Marionette:SetContext", {"value": "chrome"})
        return send(3, "WebDriver:ExecuteAsyncScript", {"script": script, "args": [], "scriptTimeout": int(timeout * 1000)})["value"]


_QUERY_DEFAULT = """
const done = arguments[arguments.length - 1];
const { SearchService } = ChromeUtils.importESModule("moz-src:///toolkit/components/search/SearchService.sys.mjs");
SearchService.init().then(async () => {
  const e = await SearchService.getDefault();
  done({name: e.name, url: e.getSubmission("camou desk").uri.spec, app: Services.appinfo.name,
        visible: (await SearchService.getVisibleEngines()).map(x => x.name)});
}).catch(err => done({error: String(err)}));
"""


@__import__("pytest").mark.live_browser
class TestLiveSearchEngine(unittest.TestCase):
    """Camoufox accepts the engine as its verified default, from the first launch on."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProfileStore(app_dir=root / "app", legacy_dir=root / "legacy")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _default_engine(self, prof) -> dict:
        import socket
        import subprocess

        from camoudesk.core.launcher import build_launch_spec

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        spec = build_launch_spec(prof, self.store, headless=True, url_override="about:blank")
        prefs_js = self.store.firefox_data_dir(prof.id) / "user.js"
        prefs_js.write_text(f'user_pref("marionette.port", {port});\n')
        proc = subprocess.Popen(
            [spec.executable, *spec.args, "-marionette", "-remote-allow-system-access"],
            env=spec.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            return _marionette(port, _QUERY_DEFAULT)
        finally:
            proc.terminate()
            proc.wait(timeout=30)
            prefs_js.unlink()

    def test_engine_is_default_on_first_and_later_launches(self) -> None:
        prof = self.store.create_profile({"name": "Live search", "os": "macos"})
        first = self._default_engine(prof)
        self.assertEqual(first.get("name"), "DuckDuckGo Search", first)
        self.assertEqual(first["url"], "https://duckduckgo.com/?q=camou+desk")
        again = self._default_engine(prof)
        self.assertEqual(again.get("name"), "DuckDuckGo Search", again)

        SettingsStore(self.store.app_dir).update({"search_engine": "google"})
        switched = self._default_engine(prof)
        self.assertEqual(switched.get("name"), "Google Search", switched)
        self.assertNotIn("DuckDuckGo Search", switched["visible"])

        SettingsStore(self.store.app_dir).update({"search_engine": "none"})
        restored = self._default_engine(prof)
        self.assertEqual(restored.get("name"), "None", restored)

    def test_profile_used_before_without_an_engine(self) -> None:
        SettingsStore(self.store.app_dir).update({"search_engine": "none"})
        prof = self.store.create_profile({"name": "Old profile", "os": "macos"})
        self.assertEqual(self._default_engine(prof).get("name"), "None")
        SettingsStore(self.store.app_dir).update({"search_engine": "duckduckgo"})
        for _ in range(2):
            result = self._default_engine(prof)
            self.assertEqual(result.get("name"), "DuckDuckGo Search", result)

    def test_without_policy_presets_only_the_first_start_is_lost(self) -> None:
        prof = self.store.create_profile({"name": "No presets", "os": "macos"})
        with patch.object(se, "policy_prefs", return_value={}):
            self.assertEqual(self._default_engine(prof).get("name"), "None")
            for _ in range(2):
                result = self._default_engine(prof)
                self.assertEqual(result.get("name"), "DuckDuckGo Search", result)
