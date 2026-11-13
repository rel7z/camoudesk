"""Native window chrome follows the app theme (ui/window_chrome.py)."""

from pathlib import Path

from camoudesk.ui import window_chrome
from camoudesk.ui.api import DesktopBridgeApi
from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager


def test_theme_maps_to_appearance_and_page_background() -> None:
    assert window_chrome.window_settings("dark") == ("NSAppearanceNameDarkAqua", (15 / 255, 17 / 255, 21 / 255))
    name, _ = window_chrome.window_settings("light")
    assert name == "NSAppearanceNameAqua"
    assert window_chrome.window_settings("system")[0] is None  # follow macOS


def test_backgrounds_match_stylesheet() -> None:
    css = (Path(window_chrome.__file__).parent / "frontend" / "styles.css").read_text()
    for theme, rgb in window_chrome.THEME_BG.items():
        assert "#%02x%02x%02x" % rgb in css.lower(), theme


def test_theme_change_notifies_listener(tmp_path: Path) -> None:
    store = ProfileStore(app_dir=tmp_path / "app", legacy_dir=tmp_path / "legacy")
    api = DesktopBridgeApi(store=store, manager=ProcessManager(store=store, adopt_running=False))
    seen = []
    api.set_theme_listener(seen.append)
    api.settings_update({"new_profile": {"humanize": False}})
    api.settings_update({"theme": "light"})
    assert seen == ["light"]
