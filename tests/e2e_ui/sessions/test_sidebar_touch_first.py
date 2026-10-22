"""Sidebar and composer on a touch-first device at desktop width.

A tablet in landscape, or a VR laser pointer driven through Chromium's touch
emulation, gets the desktop layout but cannot hover. The desktop row controls
(quick pin, quick archive, kebab) are revealed on hover, so on such a device
they used to sit invisibly over the right end of every row: a tap there
archived or pinned the session instead of opening it.

These tests run a 1280x800 context with touch enabled, which makes Chromium
report ``(pointer: coarse)`` and ``(hover: none)``, and check that:

- rows carry no hidden controls, are tall enough to tap, and a tap anywhere on
  them opens the session (and archives nothing);
- a long-press opens the row menu, which offers Pin;
- a finger drag on the session list scrolls it instead of grabbing the row;
- the composer keeps Enter as a newline but sends on Ctrl+Enter;
- a mouse at the same width still gets the hover-revealed controls.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from tests.e2e_ui.conftest import _create_runner_bound_session

_VIEWPORT = {"width": 1280, "height": 800}
_ROW_CONTROLS = ("quick-pin-conversation", "quick-archive-conversation", "conversation-actions")
# Tap targets on a coarse pointer should be at least this tall (CSS px).
_MIN_TOUCH_ROW_HEIGHT = 36

_MARK_SIDEBAR_SCROLLER = """
() => {
  let n = document.querySelector('a[href^="/c/"]');
  while (n && n !== document.documentElement) {
    const cs = getComputedStyle(n);
    const scrolls = cs.overflowY === 'auto' || cs.overflowY === 'scroll';
    if (scrolls && n.scrollHeight > n.clientHeight + 4) {
      n.setAttribute('data-test-sidebar-scroller', '');
      return true;
    }
    n = n.parentElement;
  }
  return false;
}
"""


@pytest.fixture
def touch_page(browser: Browser) -> Iterator[Page]:
    """A desktop-width page whose only pointer is a touchscreen."""
    context: BrowserContext = browser.new_context(viewport=_VIEWPORT, has_touch=True)
    try:
        yield context.new_page()
    finally:
        context.close()


def _row(page: Page, session_id: str):
    return page.locator(f'a[href="/c/{session_id}"]')


def _long_press(page: Page, locator: Locator, hold_s: float = 0.9) -> None:
    """Hold one finger still on the left part of *locator*, then lift it."""
    box = locator.bounding_box()
    assert box is not None
    point = [{"x": box["x"] + box["width"] * 0.3, "y": box["y"] + box["height"] / 2}]
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point})
    time.sleep(hold_s)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def _finger_drag(page: Page, x: float, y: float, dy: float, steps: int = 12) -> None:
    """Put a finger down at (x, y), move it *dy* px vertically, lift it."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for i in range(1, steps + 1):
        point = [{"x": x, "y": y + dy * i / steps}]
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": point})
        time.sleep(0.016)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def test_touch_rows_have_no_hidden_controls_and_tap_opens(
    touch_page: Page,
    seeded_session: tuple[str, str],
    runner_id: str,
) -> None:
    """A tap on a row's right end opens the session; nothing hides there.

    :param touch_page: 1280x800 page with a touchscreen pointer.
    :param seeded_session: ``(base_url, session_id)`` the page starts on.
    :param runner_id: Live runner, to create the session under test.
    """
    base_url, current_id = seeded_session
    target_id = _create_runner_bound_session(base_url, runner_id)
    page = touch_page
    try:
        page.goto(f"{base_url}/c/{current_id}")
        assert page.evaluate("matchMedia('(pointer: coarse)').matches")
        assert page.evaluate("matchMedia('(hover: none)').matches")

        row = _row(page, target_id)
        expect(row).to_be_visible()
        item = page.locator("li", has=row)
        for test_id in _ROW_CONTROLS:
            # Not rendered at all: an opacity-0 button would still take the tap.
            expect(item.get_by_test_id(test_id)).to_be_hidden()

        box = row.bounding_box()
        assert box is not None
        assert box["height"] >= _MIN_TOUCH_ROW_HEIGHT, box

        # Where the hover controls used to sit: the last ~70px of the row.
        x, y = box["x"] + box["width"] - 30, box["y"] + box["height"] / 2
        hit = page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.closest('a, button')"
            "?.getAttribute('href')",
            [x, y],
        )
        assert hit == f"/c/{target_id}"
        page.touchscreen.tap(x, y)
        expect(page).to_have_url(f"{base_url}/c/{target_id}")
        snap = httpx.get(f"{base_url}/v1/sessions/{target_id}", timeout=10.0).json()
        assert not snap.get("archived"), snap
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{target_id}", timeout=10.0)


def test_touch_long_press_menu_offers_pin(
    touch_page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Long-pressing a row opens its menu, and Pin is in it.

    :param touch_page: 1280x800 page with a touchscreen pointer.
    :param seeded_session: ``(base_url, session_id)`` for the row under test.
    """
    base_url, session_id = seeded_session
    page = touch_page
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()

    _long_press(page, row)
    pin = page.get_by_role("menuitem", name="Pin", exact=True)
    expect(pin).to_be_visible()
    expect(page.get_by_test_id("archive-conversation")).to_be_visible()
    pin.tap()
    expect(pin).to_be_hidden()

    # Pinning moves the row into the Pinned section; press it where it is now.
    expect(page.get_by_text("Pinned", exact=True)).to_be_visible()
    _long_press(page, row)
    expect(page.get_by_role("menuitem", name="Unpin", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page).to_have_url(f"{base_url}/c/{session_id}")


def test_touch_drag_scrolls_session_list(
    browser: Browser,
    seeded_session: tuple[str, str],
    runner_id: str,
) -> None:
    """A finger drag over the rows scrolls the list; it grabs and opens nothing.

    :param browser: Playwright browser to open a short touch context on.
    :param seeded_session: ``(base_url, session_id)`` the page starts on.
    :param runner_id: Live runner, to create enough sessions to overflow.
    """
    base_url, current_id = seeded_session
    extra = [_create_runner_bound_session(base_url, runner_id) for _ in range(24)]
    context = browser.new_context(viewport={"width": 1280, "height": 600}, has_touch=True)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{current_id}")
        expect(_row(page, extra[-1])).to_be_visible()
        assert page.evaluate(_MARK_SIDEBAR_SCROLLER), "session list should overflow"
        box = _row(page, extra[-4]).bounding_box()
        assert box is not None

        # Drag up over the rows: the list scrolls down by about the drag.
        _finger_drag(page, box["x"] + box["width"] * 0.3, box["y"] + box["height"] / 2, -250)
        page.wait_for_function(
            "() => document.querySelector('[data-test-sidebar-scroller]').scrollTop > 100",
            timeout=5000,
        )
        expect(page).to_have_url(f"{base_url}/c/{current_id}")
        assert page.evaluate("String(window.getSelection())") == ""
        dnd = "[...document.querySelectorAll('[id^=DndLiveRegion]')].map((n) => n.textContent)"
        assert "Picked up" not in " ".join(page.evaluate(dnd))
    finally:
        context.close()
        for sid in extra:
            httpx.delete(f"{base_url}/v1/sessions/{sid}", timeout=10.0)


def test_touch_composer_enter_is_newline_and_ctrl_enter_sends(
    touch_page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """With a hardware keyboard on a touch device, Ctrl+Enter still sends.

    :param touch_page: 1280x800 page with a touchscreen pointer.
    :param seeded_session: ``(base_url, session_id)`` to send in.
    """
    base_url, session_id = seeded_session
    page = touch_page
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    composer.tap()
    composer.fill("first line")
    page.keyboard.press("Enter")
    expect(composer).to_have_value("first line\n")

    page.keyboard.type("second line")
    page.keyboard.press("Control+Enter")
    expect(composer).to_have_value("")
    expect(page.get_by_text("second line")).to_be_visible()


def test_mouse_rows_keep_hover_revealed_controls(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """At the same width, a mouse still reveals the row controls on hover.

    :param page: Default (mouse) desktop page.
    :param seeded_session: ``(base_url, session_id)`` for the row under test.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    item = page.locator("li", has=row)
    kebab = item.get_by_test_id("conversation-actions")
    expect(kebab).to_have_css("opacity", "0")
    row.hover()
    expect(kebab).to_have_css("opacity", "1")
    for test_id in _ROW_CONTROLS:
        expect(item.get_by_test_id(test_id)).to_be_visible()
