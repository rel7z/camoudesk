"""Native window chrome that matches the app theme.

By default macOS draws the title bar in the system appearance, so a dark CamouDesk on a Mac in
Light mode got a light-grey strip across the top. Instead the content is drawn under a
transparent title bar (traffic lights over the sidebar, title hidden), and the window's
appearance and background follow the app's theme setting. The page provides a drag strip
(``.pywebview-drag-region``) so the window can still be moved.
"""

from typing import Any, Tuple

# Page background per theme (keep in sync with --bg in frontend/styles.css).
THEME_BG = {"dark": (0x0F, 0x11, 0x15), "light": (0xF5, 0xF6, 0xF8)}


def effective_theme(theme: str) -> str:
    """"dark" or "light"; "system" follows the current macOS appearance."""
    if theme in THEME_BG:
        return theme
    try:
        import AppKit

        name = AppKit.NSApplication.sharedApplication().effectiveAppearance().name()
        return "dark" if "Dark" in str(name) else "light"
    except Exception:
        return "dark"


def window_settings(theme: str) -> Tuple[Any, Tuple[float, float, float]]:
    """(NSAppearance name or None for "system", background RGB 0..1) for a theme."""
    eff = effective_theme(theme)
    r, g, b = THEME_BG[eff]
    return (None if theme == "system" else ("NSAppearanceNameDarkAqua" if eff == "dark" else "NSAppearanceNameAqua"),
            (r / 255.0, g / 255.0, b / 255.0))


def _apply_now(window: Any, theme: str) -> None:
    import AppKit

    nswin = getattr(window, "native", None)
    if nswin is None:
        return
    nswin.setStyleMask_(nswin.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
    nswin.setTitlebarAppearsTransparent_(True)
    nswin.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    # pywebview paints the title bar container with windowBackgroundColor (an opaque strip that
    # would sit over the page); clear it so the page shows through.
    try:
        bar = nswin.contentView().superview().subviews().lastObject()
        if bar is not None and bar.respondsToSelector_(b"setBackgroundColor:"):
            bar.setBackgroundColor_(AppKit.NSColor.clearColor())
    except Exception:
        pass
    appearance_name, (r, g, b) = window_settings(theme)
    appearance = getattr(AppKit, appearance_name) if appearance_name else None
    nswin.setAppearance_(AppKit.NSAppearance.appearanceNamed_(appearance) if appearance else None)
    nswin.setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))


def apply(window: Any, theme: str) -> None:
    """Apply chrome for ``theme``; safe to call from any thread (runs on the main thread)."""
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_apply_now, window, theme)
    except Exception:
        pass
