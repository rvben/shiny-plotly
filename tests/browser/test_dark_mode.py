"""The README's dark mode recipe: the figure follows the page's color mode."""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"
GD = "document.getElementById('sales-plotly')"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/dark/")
    expect(page.locator(f"#sales {SVG}").first).to_be_visible()
    yield page
    assert errors == []


def full_layout(page: Page, path: str) -> object:
    return page.evaluate(f"() => {GD}._fullLayout.{path}")


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
