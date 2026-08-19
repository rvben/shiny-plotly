"""extend_traces, restyle and relayout applied to drawn figures in a real browser."""

import json
import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/live/")
    expect(page.locator(f"#live {SVG}").first).to_be_visible()
    expect(page.locator(f"#late {SVG}").first).to_be_visible()
    expect(page.locator(f"#m-fig {SVG}").first).to_be_visible()
    yield page
    assert errors == []


def gd(output_id: str) -> str:
    return f"document.getElementById('{output_id}-plotly')"


def trace_y(output_id: str, index: int) -> str:
    # Array.from: plotly may hold decoded data in typed arrays, which stringify differently.
    return f"Array.from({gd(output_id)}.data[{index}].y)"


def marker_color(output_id: str, index: int) -> str:
    return f"({gd(output_id)}.data[{index}].marker || {{}}).color"


def title(output_id: str) -> str:
    return f"({gd(output_id)}.layout.title || {{}}).text"


def wait_for(page: Page, expression: str, expected: object) -> None:
    """Until ``expression`` evaluated in the page equals ``expected`` (as JSON)."""
    # Compact separators: the same text JSON.stringify produces, which is compared to.
    want = json.dumps(json.dumps(expected, separators=(",", ":")))
    page.wait_for_function(f"() => JSON.stringify({expression}) === {want}")


def value(page: Page, expression: str) -> object:
    return page.evaluate(f"() => {expression}")


def test_extend_traces_appends_to_the_named_trace_within_the_rolling_window(app: Page):
    assert value(app, trace_y("live", 0)) == [1, 2, 3]

    app.click("#tick")
    wait_for(app, trace_y("live", 0), [1, 2, 3, 4])
    app.click("#tick")
    wait_for(app, trace_y("live", 0), [2, 3, 4, 5])
    app.click("#tick")
    wait_for(app, trace_y("live", 0), [3, 4, 5, 6])

    assert value(app, trace_y("live", 1)) == [3, 2, 1], "the other trace is untouched"
    # The drawn graph follows: the fourth point was drawn, the first is gone.
    expect(app.locator("#live .scatterlayer .trace").first.locator(".point")).to_have_count(4)


def test_restyle_changes_only_the_named_trace_and_the_drawing_follows(app: Page):
    app.click("#recolor")

    wait_for(app, marker_color("live", 1), "rgb(255, 0, 0)")
    assert value(app, marker_color("live", 0)) is None
    second = app.locator("#live .scatterlayer .trace").nth(1).locator(".point").first
    expect(second).to_have_attribute("style", re.compile(r"fill: rgb\(255, 0, 0\)"))


def test_relayout_changes_the_layout_and_the_drawing_follows(app: Page):
    app.click("#retitle")

    wait_for(app, title("live"), "title 1")
    expect(app.locator("#live .gtitle")).to_have_text("title 1")


def test_a_re_render_replaces_the_figure_updates_included(app: Page):
    app.click("#tick")
    app.click("#recolor")
    app.click("#retitle")
    wait_for(app, title("live"), "title 1")
    wait_for(app, trace_y("live", 0), [1, 2, 3, 4])

    app.click("#redraw")

    wait_for(app, trace_y("live", 0), [1, 2, 3])
    assert value(app, marker_color("live", 1)) is None
    assert value(app, title("live")) is None
    expect(app.locator("#live .gtitle")).to_have_count(0)


def test_updates_sent_before_the_first_draw_are_applied_right_after_it(app: Page):
    # The server sent these while the figure was still being computed.
    wait_for(app, trace_y("late", 0), [1, 9])
    wait_for(app, title("late"), "queued")
    expect(app.locator("#late .gtitle")).to_have_text("queued")


def test_a_module_update_reaches_the_module_output_only(app: Page):
    app.click("#m-recolor")

    wait_for(app, marker_color("m-fig", 0), "rgb(255, 0, 0)")
    assert value(app, marker_color("live", 0)) is None
    assert value(app, marker_color("live", 1)) is None


def test_an_update_for_an_output_that_is_not_on_the_page_is_dropped_with_a_warning(app: Page):
    with app.expect_console_message(lambda m: m.type == "warning" and "absent" in m.text) as info:
        app.click("#nowhere")

    assert "relayout" in info.value.text
    # Nothing else changed, and the page is still fine (no errors: the fixture checks).
    assert value(app, title("live")) is None


def test_a_fixed_height_output_on_a_plain_page_keeps_the_graph_inside_it(app: Page):
    """page_fluid brings no fill CSS of its own; the output must, or the graph overflows."""
    output = app.locator("#m-fig").bounding_box()
    # The drawn SVG, not plotly's wrapper div: the wrapper has a version-dependent height.
    graph = app.locator("#m-fig .main-svg").first.bounding_box()

    assert output is not None and graph is not None
    assert output["height"] == 200
    assert graph["height"] == 200
    assert graph["width"] == 300
