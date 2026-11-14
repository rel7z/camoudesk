"""PyWebView macOS desktop host entrypoint for CamouDesk."""

from pathlib import Path
import sys
import threading

import webview

from camoudesk.core.storage import ProfileStore
from camoudesk.services.context import AppContext
from camoudesk.ui import window_chrome
from camoudesk.ui.api import DesktopBridgeApi
from camoudesk.ui.single_instance import InstanceLock, activate_existing

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def main() -> None:
    store = ProfileStore()
    instance = InstanceLock(store.app_dir)
    if not instance.acquire():
        # Another CamouDesk already manages this data folder: show it instead of competing.
        activate_existing(instance.holder_pid())
        print("CamouDesk is already running; brought the existing window to the front.", file=sys.stderr)
        return
    ctx = AppContext.create(store=store)  # stores, process manager, event bus, services
    manager = ctx.manager
    api = DesktopBridgeApi(ctx=ctx)

    index_file = FRONTEND_DIR / "index.html"
    window = webview.create_window(
        title="CamouDesk",
        # Served by pywebview's loopback HTTP server (see start below): WebKit will not load
        # ES modules from file:// URLs. Bridge calls still use pywebview's native channel.
        url=str(index_file.resolve()),
        js_api=api,
        width=1280,
        height=800,
        min_size=(1040, 640),
        background_color="#0f1115",
        text_select=True,
    )
    assert window is not None  # create_window returns the window it created

    def _push_status(**_: object) -> None:
        # Called from background threads whenever a browser starts, stops, exits or warns.
        try:
            window.evaluate_js("window.camoudesk && window.camoudesk.onStatusChange && window.camoudesk.onStatusChange()")
        except Exception:
            pass

    def _push_proxies(**_: object) -> None:
        try:
            window.evaluate_js("window.camoudesk && window.camoudesk.onProxiesChange && window.camoudesk.onProxiesChange()")
        except Exception:
            pass

    def _apply_chrome(*_args: object) -> None:
        # Title bar blends into the page and follows the theme (see ui/window_chrome.py).
        window_chrome.apply(window, api.settings.get().get("theme", "dark"))

    window.events.before_show += _apply_chrome
    window.events.shown += _apply_chrome

    api.set_window(window)
    ctx.events.subscribe("theme", lambda theme: window_chrome.apply(window, theme))
    ctx.events.subscribe("status", _push_status)
    ctx.events.subscribe("proxies", _push_proxies)
    manager.start_monitor()
    api.sync_api_server()  # local REST API, if enabled in Settings

    def _warm_host_gpu() -> None:
        # Learn this Mac's GPU once (headless, ~10 s, stored per Camoufox build) so profiles that
        # claim this Mac's OS can show their real canvas. See core/host_gpu.py.
        ctx.engine.prepare_host(store.app_dir)

    threading.Thread(target=_warm_host_gpu, name="camoudesk-host-gpu", daemon=True).start()

    def _on_closing() -> None:
        api.shutdown_api_server()
        manager.stop_monitor()
        manager.stop_all()

    window.events.closing += _on_closing
    webview.start(debug=False, http_server=True, private_mode=True)


if __name__ == "__main__":
    main()
