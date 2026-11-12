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
        title="CamouDesk — Open-Source Anti-Detect Manager",
        url=index_file.resolve().as_uri(),
        js_api=api,
        width=1160,
        height=760,
        min_size=(960, 620),
        background_color="#0d1017",
        text_select=True,
    )

    def _push_status() -> None:
        # Called from background threads whenever a browser starts, stops, exits or warns.
        try:
            window.evaluate_js("window.camoudesk && window.camoudesk.onStatusChange && window.camoudesk.onStatusChange()")
        except Exception:
            pass

    manager.add_listener(_push_status)
    manager.start_monitor()

    def _on_closing() -> None:
        manager.stop_monitor()
        manager.stop_all()

    window.events.closing += _on_closing
    webview.start(debug=False)


if __name__ == "__main__":
    main()
