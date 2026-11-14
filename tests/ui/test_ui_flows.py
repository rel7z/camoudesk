"""End-to-end UI tests: Camoufox (headless) drives the real frontend against the mock bridge
(``index.html?mock``, frontend/dev/mock-api.js). Run with ``pytest -m ui``; they need the
Camoufox browser (``python -m camoufox fetch``) but no network and no app data.

Each test covers a flow that once broke in a way unit tests could not see.
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from typing import Any, Iterator

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "src" / "camoudesk" / "ui" / "frontend"

pytestmark = pytest.mark.ui


class _Quiet(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(FRONTEND)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/index.html?mock"
    server.shutdown()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    from camoufox.sync_api import Camoufox

    # main_world_eval: Camoufox runs Playwright's scripts in an isolated world (its stealth); tests
    # that read the page's own globals (the mock bridge) opt in with the "mw:" prefix.
    with Camoufox(headless=True, i_know_what_im_doing=True, os="macos", window=(1280, 860), main_world_eval=True) as b:
        yield b


@pytest.fixture()
def page(browser: Any, base_url: str) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 1280, "height": 860})
    pg = context.new_page()
    pg.goto(base_url)
    pg.wait_for_selector("tbody tr")
    yield pg
    context.close()


def click_text(page: Any, selector: str, text: str) -> None:
    page.locator(selector, has_text=text).first.click()


def test_add_proxy_saves_the_line_as_typed_even_when_enter_comes_at_once(page: Any) -> None:
    """Regression: Save used the text read 200 ms earlier, storing a cut-off password."""
    page.evaluate("""mw:() => { window.__created = [];
      const orig = window.pywebview.api.proxies_create;
      window.pywebview.api.proxies_create = (d) => { window.__created.push(d); return orig(d); }; }""")
    page.keyboard.press("Meta+2")
    click_text(page, "button", "Add proxy")
    field = page.locator(".proxy-line-input")
    field.fill("core-residential.evomi.com:1000:acct:Dummy_PW_abcdefgh")
    page.wait_for_selector(".parsed")
    field.fill("core-residential.evomi.com:1000:acct:Dummy_PW_abcdefghij")
    field.press("Enter")  # before the typing delay has read the last edit
    for _ in range(50):  # wait_for_function ignores the "mw:" prefix: poll the page's own global
        if page.evaluate("mw:(window.__created || []).length"):
            break
        page.wait_for_timeout(100)
    sent = page.evaluate("mw:window.__created[0]")
    assert sent["password"] == "Dummy_PW_abcdefghij"
    assert sent["host"] == "core-residential.evomi.com"


def test_pasting_several_lines_offers_an_import(page: Any) -> None:
    page.keyboard.press("Meta+2")
    click_text(page, "button", "Add proxy")
    page.locator(".proxy-line-input").fill("1.2.3.4:8000:u:p\n5.6.7.8:9000:u2:p2\nhttp://user:pw@9.9.9.9:3128")
    page.wait_for_selector(".drawer-foot button:has-text('Import 3 proxies')")


def test_randomize_match_this_mac_previews_a_real_canvas_device(page: Any) -> None:
    click_text(page, "button", "New profile")
    click_text(page, ".drawer button", "Randomize")
    click_text(page, ".menu-pop button", "Match this Mac")
    page.wait_for_function("document.querySelector('.drawer select').value === 'macos'")
    page.locator("[role=tab]", has_text="Fingerprint").click()
    card = page.locator(".device-card .kv-card")
    card.wait_for()
    assert "Real — matches this Mac" in card.inner_text()
    assert page.locator(".drawer input").first.input_value()  # a random name was filled in


def test_evomi_region_and_city_dropdowns_follow_the_country(page: Any) -> None:
    page.locator("tbody tr", has_text="Evomi DE 01").locator(".name").click()
    page.locator("[role=tab]", has_text="Network").click()
    box = page.locator(".provider-box")
    box.locator("select").first.select_option("ID")
    cities = box.locator("select").nth(2).locator("option").all_inner_texts()
    assert "Jakarta" in cities and "Type another…" in cities


def test_row_menu_opens_fully_inside_the_window(page: Any) -> None:
    """Regression: the ⋯ menu was clipped by the table and scrolled the rows away."""
    page.set_viewport_size({"width": 1100, "height": 560})
    page.wait_for_timeout(400)  # menus close on resize (by design): let the resize land first
    last = page.locator("tbody tr").last
    last.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    last.locator(".col-actions .menu button").click()
    menu = page.locator(".menu-pop")
    menu.wait_for()
    box = menu.bounding_box()
    assert box is not None and box["y"] >= 40 and box["y"] + box["height"] <= 560
    assert page.evaluate("document.querySelector('.table-card').scrollTop") == 0


def test_check_tab_shows_a_graded_report(page: Any) -> None:
    page.locator("tbody tr", has_text="Berlin research").locator(".name").click()
    page.locator("[role=tab]", has_text="Check").click()
    click_text(page, ".drawer button", "Check anonymity")
    page.wait_for_selector(".check-summary", timeout=10000)
    statuses = page.locator(".check-item").evaluate_all("els => els.map(e => e.className)")
    assert len(statuses) == 14
    assert page.locator("tbody tr", has_text="Berlin research").locator(".pill-check").is_visible()
