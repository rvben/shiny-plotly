"""Plotly events arriving as Shiny inputs, driven by a real mouse in a real browser."""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/events/")
    expect(page.locator(f"#fig {SVG}").first).to_be_visible()
    expect(page.locator(f"#sel {SVG}").first).to_be_visible()
    expect(page.locator(f"#m-fig {SVG}").first).to_be_visible()
    expect(page.locator("#click_out")).to_have_text("-")
    yield page
    assert errors == []


def received(page: Page, output_id: str) -> object:
    text = page.locator(f"#{output_id}").inner_text()
    return None if text == "-" else json.loads(text)


def wait_for_change(page: Page, output_id: str, previous: str) -> None:
    expect(page.locator(f"#{output_id}")).not_to_have_text(previous)


def bar(page: Page, output_id: str, index: int) -> Locator:
    return page.locator(f"#{output_id} .bars .point").nth(index)


def test_click_hands_the_point_to_the_input_with_plain_customdata(app: Page):
    bar(app, "fig", 1).click(force=True)

    wait_for_change(app, "click_out", "-")
    event = received(app, "click_out")
    assert isinstance(event, dict)
    point = event["points"][0]
    assert point["curveNumber"] == 0
    assert point["pointNumber"] == 1
    assert point["x"] == "x1"
    assert point["y"] == 2
    assert point["customdata"] == [2, 3], "2-D bdata came back as a list, not a typed array"
    assert not {"data", "fullData", "xaxis", "yaxis"} & point.keys(), "no circular plotly objects"


def test_clicking_the_same_point_twice_fires_twice(app: Page):
    bar(app, "fig", 0).click(force=True)
    expect(app.locator("#click_count")).to_have_text("1")

    bar(app, "fig", 0).click(force=True)

    expect(app.locator("#click_count")).to_have_text("2")


def test_handlers_survive_a_rerender_and_are_not_stacked(app: Page):
    app.evaluate("Shiny.setInputValue('n', 5)")
    expect(app.locator("#fig .bars .point")).to_have_count(5)
    app.evaluate("Shiny.setInputValue('n', 6)")
    expect(app.locator("#fig .bars .point")).to_have_count(6)

    bar(app, "fig", 4).click(force=True)

    expect(app.locator("#click_count")).to_have_text("1")
    app.wait_for_timeout(300)
    expect(app.locator("#click_count")).to_have_text("1")
    event = received(app, "click_out")
    assert isinstance(event, dict)
    assert event["points"][0]["x"] == "x4"


def test_hover_reports_the_point_and_null_once_the_pointer_leaves(app: Page):
    bar(app, "fig", 2).hover(force=True)

    wait_for_change(app, "hover_out", "-")
    event = received(app, "hover_out")
    assert isinstance(event, dict)
    assert event["points"][0]["pointNumber"] == 2

    app.mouse.move(0, 0)

    expect(app.locator("#hover_out")).to_have_text("null")


def drag(page: Page, output_id: str, x0: float, y0: float, x1: float, y1: float) -> None:
    box = page.locator(f"#{output_id} .nsewdrag").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] * x0, box["y"] + box["height"] * y0)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * x1, box["y"] + box["height"] * y1, steps=8)
    page.mouse.up()


def test_a_dragged_zoom_arrives_as_relayout_data(app: Page):
    drag(app, "fig", 0.2, 0.2, 0.6, 0.7)

    wait_for_change(app, "relayout_out", "-")
    event = received(app, "relayout_out")
    assert isinstance(event, dict)
    assert "xaxis.range[0]" in event and "yaxis.range[1]" in event


def test_a_box_selection_arrives_with_points_and_range_and_clears_on_deselect(app: Page):
    drag(app, "sel", 0.05, 0.05, 0.6, 0.95)

    wait_for_change(app, "selected_out", "-")
    event = received(app, "selected_out")
    assert isinstance(event, dict)
    assert [p["pointNumber"] for p in event["points"]] == [0, 1]
    assert set(event["range"]) == {"x", "y"}

    box = app.locator("#sel .nsewdrag").bounding_box()
    assert box is not None
    app.mouse.dblclick(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.5)

    expect(app.locator("#selected_out")).to_have_text("null")


def test_inputs_are_namespaced_inside_a_module(app: Page):
    bar(app, "m-fig", 2).click(force=True)

    expect(app.locator("#m-out")).to_have_text('"x2"')
    # The outer plot, whose output is also called "fig", saw nothing.
    expect(app.locator("#click_count")).to_have_text("0")
