"""The web page may call only the bridge's own methods.

pywebview exposes every public method of ``js_api`` and walks into nested non-callable objects
(webview/util.py get_functions), skipping objects whose ``_serializable`` is False. Stores, the
process manager and the AppContext must never become callable from the page: that would bypass
the bridge's validation (e.g. ``api.store.delete_profile``)."""

import inspect
from pathlib import Path
import tempfile
import unittest

from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.ui.api import DesktopBridgeApi


def exposed(obj: object, base: str = "", seen=None, out=None):  # type: ignore[no-untyped-def]
    """Mirror of pywebview's get_functions walk."""
    seen = set() if seen is None else seen
    out = {} if out is None else out
    if id(obj) in seen:
        return out
    seen.add(id(obj))
    for name in dir(obj):
        if name.startswith("_"):
            continue
        full = f"{base}.{name}" if base else name
        try:
            attr = getattr(obj, name)
        except Exception:
            continue
        if not getattr(attr, "_serializable", True):
            continue
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            out[full] = True
        elif inspect.isclass(attr) or (not callable(attr) and hasattr(attr, "__module__")):
            exposed(attr, full, seen, out)
    return out


class TestJsApiSurface(unittest.TestCase):
    def test_only_bridge_methods_are_callable_from_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(app_dir=Path(tmp) / "app", legacy_dir=Path(tmp) / "legacy")
            api = DesktopBridgeApi(store=store, manager=ProcessManager(store=store, adopt_running=False))
            names = exposed(api)
        nested = sorted(n for n in names if "." in n)
        self.assertEqual(nested, [], f"nested objects exposed to the page: {nested[:10]}")
        for name in ("profiles_list", "proxies_list", "profiles_check_identity", "settings_update"):
            self.assertIn(name, names)
        for internal in ("store", "manager", "ctx", "proxies", "settings", "providers"):
            self.assertNotIn(internal, names)


if __name__ == "__main__":
    unittest.main()
