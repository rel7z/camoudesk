"""PyWebView macOS desktop host entrypoint for CamouDesk."""

from pathlib import Path

import webview

from camoudesk.core.storage import ProfileStore
from camoudesk.manager.process_manager import ProcessManager
from camoudesk.ui.api import DesktopBridgeApi

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def main() -> None:
    store = ProfileStore()
    manager = ProcessManager(store=store)
    api = DesktopBridgeApi(store=store, manager=manager)

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

    def _push_status() -> None:
        # Called from background threads whenever a browser starts, stops, exits or warns.
        try:
            window.evaluate_js("window.camoudesk && window.camoudesk.onStatusChange && window.camoudesk.onStatusChange()")
        except Exception:
            pass

    def _push_proxies() -> None:
        try:
            window.evaluate_js("window.camoudesk && window.camoudesk.onProxiesChange && window.camoudesk.onProxiesChange()")
        except Exception:
            pass

    api.set_window(window)
    manager.add_listener(_push_status)
    api.set_proxy_notifier(_push_proxies)
    manager.start_monitor()
    api.sync_api_server()  # local REST API, if enabled in Settings

    def _on_closing() -> None:
        api.shutdown_api_server()
        manager.stop_monitor()
        manager.stop_all()

    window.events.closing += _on_closing
    webview.start(debug=False, http_server=True, private_mode=True)


if __name__ == "__main__":
    main()
