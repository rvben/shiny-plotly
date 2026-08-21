"""Dark mode: the README's re-render recipe and the theme option's client-side flip."""

import json
import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from shiny_plotly._render import TEMPLATE_MESSAGE

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"
GD = "document.getElementById('sales-plotly')"
AUTO = "document.getElementById('auto-plotly')"

LIGHT_TEXT = "#2a3f5f"  # the plotly template's font color
DARK_TEXT = "#f2f5fa"  # the plotly_dark template's font color

T, V = "templates", "values"  # the two kinds of websocket frame a themed page depends on


def kind(frame: str) -> str:
    message = json.loads(frame)
    if TEMPLATE_MESSAGE in (message.get("custom") or {}):
        return T
    return V if "values" in message else "other"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/dark/")
    expect(page.locator(f"#sales {SVG}").first).to_be_visible()
    expect(page.locator(f"#auto {SVG}").first).to_be_visible()
    yield page
    assert errors == []


def full_layout(page: Page, path: str, gd: str = GD) -> object:
    return page.evaluate(f"() => {gd}._fullLayout.{path}")


def test_the_figure_follows_the_page_color_mode(app: Page):
    assert app.evaluate("document.documentElement.dataset.bsTheme") == "light"
    assert full_layout(app, "font.color") == "#2a3f5f", "the plotly template's text color"
    assert full_layout(app, "paper_bgcolor") == "rgba(0, 0, 0, 0)", "the page shows through"
    assert full_layout(app, "plot_bgcolor") == "rgba(0, 0, 0, 0)"

    app.locator("#mode").locator("button").first.click()  # the switch, in its shadow root

    app.wait_for_function("() => document.documentElement.dataset.bsTheme === 'dark'")
    app.wait_for_function(f"() => {GD}._fullLayout.font.color === '#f2f5fa'")  # plotly_dark
    assert full_layout(app, "paper_bgcolor") == "rgba(0, 0, 0, 0)"
    assert full_layout(app, "plot_bgcolor") == "rgba(0, 0, 0, 0)"
    # The drawn graph follows: axis tick labels are light on the dark page.
    tick = app.locator("#sales .xtick text").first
    expect(tick).to_have_attribute("style", re.compile(r"fill: rgb\(242, 245, 250\)"))


def test_theme_auto_follows_the_dark_mode_input_in_the_browser(app: Page):
    assert full_layout(app, "font.color", AUTO) == LIGHT_TEXT
    assert full_layout(app, "paper_bgcolor", AUTO) == "rgba(0, 0, 0, 0)", "the page shows through"
    assert full_layout(app, "plot_bgcolor", AUTO) == "rgba(0, 0, 0, 0)"

    app.locator("#mode").locator("button").first.click()

    # The render function cannot see the mode input, so this flip happened client-side.
    app.wait_for_function(f"() => {AUTO}._fullLayout.font.color === '{DARK_TEXT}'")
    assert full_layout(app, "paper_bgcolor", AUTO) == "rgba(0, 0, 0, 0)"
    tick = app.locator("#auto .xtick text").first
    expect(tick).to_have_attribute("style", re.compile(r"fill: rgb\(242, 245, 250\)"))

    app.locator("#mode").locator("button").first.click()

    app.wait_for_function(f"() => {AUTO}._fullLayout.font.color === '{LIGHT_TEXT}'")


def test_a_re_render_keeps_the_mode_that_is_active_at_that_moment(app: Page):
    app.locator("#mode").locator("button").first.click()
    app.wait_for_function(f"() => {AUTO}._fullLayout.font.color === '{DARK_TEXT}'")

    app.click("#redraw_auto")

    # The re-render changes the bar count, so its arrival is observable; the fresh
    # figure must come up in the dark template, not fall back to light.
    app.wait_for_function(f"() => {AUTO}.data[0].y.length === 4")
    assert full_layout(app, "font.color", AUTO) == DARK_TEXT
    assert full_layout(app, "paper_bgcolor", AUTO) == "rgba(0, 0, 0, 0)"


def test_the_templates_travel_once_for_a_whole_page(page: Page, server_url: str):
    """Two charts on one theme: one copy of it arrives, ahead of the values that name it."""
    frames: list[str] = []

    def watch(ws):
        ws.on("framereceived", lambda payload: frames.append(payload))

    page.on("websocket", watch)

    page.goto(server_url + "/scoped/")
    expect(page.locator(f"#page {SVG}").first).to_be_visible()
    expect(page.locator(f"#panel {SVG}").first).to_be_visible()

    kinds = [kind(frame) for frame in frames]
    sent = [json.loads(f)["custom"][TEMPLATE_MESSAGE]["templates"] for f in frames if kind(f) == T]
    assert kinds.count(T) == 1, "one message, for both charts"
    assert len(sent[0]) == 2, "the light and the dark template, once each"
    assert kinds.index(T) < kinds.index(V), "in the browser before it is asked to draw with them"
    drawn = {}
    for frame in frames:
        if kind(frame) == V:
            drawn.update(json.loads(frame)["values"])
    assert drawn["page"]["theme_keys"] == drawn["panel"]["theme_keys"], "one theme, one key pair"
    assert set(drawn["page"]["theme_keys"].values()) == set(sent[0])
    assert drawn["page"]["themes"] is None, "the templates are named, not carried"
    assert drawn["panel"]["themes"] is None


def test_a_panel_can_set_the_color_mode_for_the_charts_inside_it(
    page: Page, server_url: str, errors: list[str]
):
    """data-bs-theme is allowed on any element, so the nearest ancestor decides the mode."""
    outside = "document.getElementById('page-plotly')"
    inside = "document.getElementById('panel-plotly')"
    page.goto(server_url + "/scoped/")
    expect(page.locator(f"#page {SVG}").first).to_be_visible()
    expect(page.locator(f"#panel {SVG}").first).to_be_visible()

    assert page.evaluate(f"() => {outside}._fullLayout.font.color") == LIGHT_TEXT
    assert page.evaluate(f"() => {inside}._fullLayout.font.color") == DARK_TEXT, "in the panel"

    page.evaluate("document.getElementById('dark_panel').setAttribute('data-bs-theme', 'light')")

    page.wait_for_function(f"() => {inside}._fullLayout.font.color === '{LIGHT_TEXT}'")
    assert page.evaluate(f"() => {outside}._fullLayout.font.color") == LIGHT_TEXT, "left alone"

    page.locator("#mode").locator("button").first.click()  # the page goes dark

    page.wait_for_function(f"() => {outside}._fullLayout.font.color === '{DARK_TEXT}'")
    assert page.evaluate(f"() => {inside}._fullLayout.font.color") == LIGHT_TEXT, "the panel wins"
    assert errors == []


def test_a_control_of_your_own_can_drive_the_mode(page: Page, server_url: str, errors: list[str]):
    """The README's recipe: nothing but data-bs-theme is needed to switch every chart."""
    own = "document.getElementById('own-plotly')"
    page.goto(server_url + "/own-mode/")
    expect(page.locator(f"#own {SVG}").first).to_be_visible()
    assert page.evaluate(f"() => {own}._fullLayout.font.color") == LIGHT_TEXT

    page.click("#night")

    page.wait_for_function(f"() => {own}._fullLayout.font.color === '{DARK_TEXT}'")

    page.click("#night")

    page.wait_for_function(f"() => {own}._fullLayout.font.color === '{LIGHT_TEXT}'")
    assert errors == []


def test_a_custom_pair_follows_the_os_preference_when_the_page_has_no_mode_input(
    page: Page, server_url: str, errors: list[str]
):
    styled = "document.getElementById('styled-plotly')"
    page.emulate_media(color_scheme="light")
    page.goto(server_url + "/theme/")
    expect(page.locator(f"#styled {SVG}").first).to_be_visible()

    assert page.evaluate("document.documentElement.getAttribute('data-bs-theme')") is None
    assert page.evaluate(f"() => {styled}._fullLayout.font.color") == "rgb(36,36,36)", "seaborn"

    page.emulate_media(color_scheme="dark")

    page.wait_for_function(f"() => {styled}._fullLayout.font.color === 'rgb(200, 100, 50)'")
    assert page.evaluate(f"() => {styled}._fullLayout.paper_bgcolor") == "rgba(0, 0, 0, 0)"

    page.emulate_media(color_scheme="light")

    page.wait_for_function(f"() => {styled}._fullLayout.font.color === 'rgb(36,36,36)'")
    assert errors == []
