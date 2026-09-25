"""In-place updates to figures built from numpy arrays, which plotly 6 sends as binary."""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/arrays/")
    for output_id in ("ints", "floats", "two", "grid"):
        expect(page.locator(f"#{output_id} {SVG}").first).to_be_visible()
    yield page
    assert errors == []


def drawn(output_id: str, index: int, attribute: str) -> str:
    """The values plotly draws for one attribute of one trace, as a plain list."""
    trace = f"document.getElementById('{output_id}-plotly')._fullData[{index}]"
    return f"Array.from({trace}.{attribute})"


def wait_for(page: Page, expression: str, expected: object) -> None:
    want = json.dumps(json.dumps(expected, separators=(",", ":")))
    page.wait_for_function(f"() => JSON.stringify({expression}) === {want}")


def points(page: Page, output_id: str, index: int) -> int:
    return page.locator(f"#{output_id} .scatterlayer .trace").nth(index).locator(".point").count()


def test_a_trace_built_from_numpy_takes_points_of_any_value(app: Page):
    """np.arange(3) travels as int8; a 300 and a 0.5 must still land as themselves."""
    app.click("#extend_plain")

    wait_for(app, drawn("ints", 0, "y"), [0, 1, 2, 300, 0.5])
    assert app.evaluate(f"() => {drawn('ints', 0, 'x')}") == [0, 1, 2, 3, 4]
    assert points(app, "ints", 0) == 5, "the drawing follows the data"


def test_extending_a_numpy_trace_keeps_the_rolling_window(app: Page):
    app.click("#extend_capped")

    wait_for(app, drawn("floats", 0, "y"), [1.5, 2.5, 3.5])
    assert app.evaluate(f"() => {drawn('floats', 0, 'x')}") == [0.5, 1, 2]


def test_prepending_reaches_numpy_and_list_traces_alike(app: Page):
    app.click("#prepend")

    wait_for(app, drawn("two", 0, "y"), [-1, 0, 1])
    wait_for(app, drawn("two", 1, "y"), [4, 5, 6])


def test_numpy_points_of_any_dtype_land_as_their_values(app: Page):
    """uint8, an int64 past int32 and a float32, into int8 and float64 traces."""
    app.click("#extend_numpy")

    wait_for(app, drawn("ints", 0, "y"), [0, 1, 2, 2**40, -1, 7])
    assert app.evaluate(f"() => {drawn('ints', 0, 'x')}") == [0, 1, 2, 5, 6, 7]
    wait_for(app, drawn("floats", 0, "y"), [0.5, 1.5, 2.5, 0.25])


def test_numpy_arrays_in_restyle_add_traces_and_update_land_as_their_values(app: Page):
    app.click("#replace")

    wait_for(app, drawn("floats", 0, "y"), [9.5, 10.5])
    wait_for(app, drawn("two", 2, "y"), [-3, 3])
    wait_for(app, drawn("two", 1, "marker.size"), [11, 12])


def test_updates_held_for_a_hidden_numpy_figure_apply_once_it_is_drawn(app: Page):
    app.click("#burst")
    expect(app.locator("#bursts")).to_have_text("bursts 1")

    app.get_by_role("tab", name="Held").click()
    expect(app.locator(f"#held {SVG}").first).to_be_visible()

    wait_for(app, drawn("held", 0, "y"), [2, 0.5, 10.5, 20.5, 30.5, 40.5])


def test_a_heatmap_built_from_a_2d_array_takes_a_new_row(app: Page):
    app.click("#add_row")

    rows = "document.getElementById('grid-plotly')._fullData[0].z.map((r) => Array.from(r))"
    wait_for(app, rows, [[0, 1], [2, 3], [40, 50.5]])
