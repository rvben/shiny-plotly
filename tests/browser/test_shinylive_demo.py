"""The shinylive demo app, served as is: what ships to Pages works before it ships."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

GD = "document.querySelector('#stream .plotly-graph-div')"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/demo/")
    expect(page.locator("#stream svg.main-svg").first).to_be_visible()
    yield page
    assert errors == []


def test_the_stream_tab_accumulates_points(app: Page):
    app.wait_for_function(
        f"() => {{ const gd = {GD}; return gd && gd.data && gd.data[0].x.length >= 3; }}"
    )

    assert app.evaluate(f"{GD}.data[1].x.length") >= 3, "both traces stream"


def test_the_explore_tab_reports_a_click_and_a_selection(app: Page):
    app.get_by_role("tab", name="Explore").click()
    expect(app.locator("#explore svg.main-svg").first).to_be_visible()
    expect(app.locator("#clicked")).to_have_text("Click a point.")

    app.locator("#explore .scatterlayer .point").first.click(force=True)

    expect(app.locator("#clicked")).not_to_have_text("Click a point.")

    # The figure starts in select mode, so a plain drag is a box selection.
    drag_area = app.locator("#explore .nsewdrag")
    drag_area.scroll_into_view_if_needed()
    box = drag_area.bounding_box()
    assert box is not None
    app.mouse.move(box["x"] + box["width"] * 0.1, box["y"] + box["height"] * 0.1)
    app.mouse.down()
    app.mouse.move(box["x"] + box["width"] * 0.9, box["y"] + box["height"] * 0.9, steps=8)
    app.mouse.up()

    expect(app.locator("#picked")).to_contain_text("points selected")
