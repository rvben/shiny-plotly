"""render_plotly(resample=...) in a real browser: the overview drawn first, a closer
sample for every zoom, the overview again on reset, and nothing stale ever drawn."""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from .apps import BIG, RESAMPLE_BUDGET, SPIKE_AT

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/resample/")
    for output_id in ("big", "dates", "logx", "clicks"):
        expect(page.locator(f"#{output_id} {SVG}").first).to_be_visible()
    expect(page.locator("#reports")).to_have_text("reports 0")
    yield page
    assert errors == []


def gd(output_id: str) -> str:
    return f"document.getElementById('{output_id}-plotly')"


def drawn(page: Page, output_id: str, index: int, attribute: str) -> list:
    return page.evaluate(f"() => Array.from({gd(output_id)}._fullData[{index}].{attribute})")


def relayout(page: Page, output_id: str, update: dict) -> None:
    page.evaluate(f"() => Plotly.relayout({gd(output_id)}, {json.dumps(update)})")


def wait_for_x(page: Page, output_id: str, first, last, index: int = 0) -> None:
    """Until the trace draws x from ``first`` to ``last``."""
    x = f"{gd(output_id)}._fullData[{index}].x"
    ends = f"[{x}[0], {x}[{x}.length - 1]]"
    want = json.dumps([first, last], separators=(",", ":"))
    if isinstance(first, str):
        # Plotly 5 omits zero seconds; Plotly 6 includes them. Compare actual instants.
        ends += ".map(value => Date.parse(value))"
        want += ".map(value => Date.parse(value))"
    page.wait_for_function(f"() => JSON.stringify({ends}) === JSON.stringify({want})")


def reports(page: Page) -> int:
    return int(page.locator("#reports").inner_text().split()[1])


def test_the_overview_is_a_bounded_sample_that_keeps_the_spike(app: Page):
    x = drawn(app, "big", 0, "x")
    y = drawn(app, "big", 0, "y")

    assert len(x) <= RESAMPLE_BUDGET
    assert x[0] == 0 and x[-1] == BIG - 1
    assert max(y) == 50.0 and x[y.index(50.0)] == SPIKE_AT
    assert drawn(app, "big", 1, "x") == [0, BIG - 1], "the short trace as it was"
    assert len(drawn(app, "big", 2, "x")) <= RESAMPLE_BUDGET


def test_a_zoom_draws_every_point_in_view_and_one_past_each_edge(app: Page):
    relayout(app, "big", {"xaxis.range": [40_000.5, 40_100.5]})

    wait_for_x(app, "big", 40_000, 40_101)
    assert drawn(app, "big", 0, "x") == list(range(40_000, 40_102))
    assert drawn(app, "big", 0, "customdata") == list(range(40_000, 40_102))
    wait_for_x(app, "big", 40_000, 40_101, index=2)
    assert drawn(app, "big", 1, "x") == [0, BIG - 1], "the short trace untouched"


def test_a_wide_zoom_stays_within_the_budget(app: Page):
    relayout(app, "big", {"xaxis.range": [10_000, 60_000]})

    wait_for_x(app, "big", 9_999, 60_001)
    assert len(drawn(app, "big", 0, "x")) <= RESAMPLE_BUDGET


def test_resetting_the_view_draws_the_overview_again(app: Page):
    overview = drawn(app, "big", 0, "x")
    relayout(app, "big", {"xaxis.range": [40_000.5, 40_100.5]})
    wait_for_x(app, "big", 40_000, 40_101)

    relayout(app, "big", {"xaxis.autorange": True})

    wait_for_x(app, "big", 0, BIG - 1)
    assert drawn(app, "big", 0, "x") == overview


def test_a_reversed_axis_is_sampled_over_its_range(app: Page):
    relayout(app, "big", {"xaxis.range": [40_100.5, 40_000.5]})

    wait_for_x(app, "big", 40_000, 40_101)


def test_a_date_axis_zoom_draws_the_minutes_in_view(app: Page):
    relayout(app, "dates", {"xaxis.range": ["2026-01-02 10:00:30", "2026-01-02 10:30:30"]})

    wait_for_x(app, "dates", "2026-01-02T10:00:00", "2026-01-02T10:31:00")
    assert len(drawn(app, "dates", 0, "x")) == 32


def test_a_log_axis_zoom_draws_the_values_in_view(app: Page):
    relayout(app, "logx", {"xaxis.range": [3, 3.01]})  # 1000 to 1023.3

    wait_for_x(app, "logx", 999, 1024)
    assert drawn(app, "logx", 0, "x") == list(range(999, 1025))


def test_a_click_reports_the_point_by_its_place_in_the_full_data(app: Page):
    relayout(app, "clicks", {"xaxis.range": [2000.5, 2004.5]})
    wait_for_x(app, "clicks", 2000, 2005)

    app.locator("#clicks .scatterlayer .trace .point").nth(3).click(force=True)

    expect(app.locator("#click_out")).not_to_have_text("-")
    point = json.loads(app.locator("#click_out").inner_text())
    assert point["pointNumber"] == point["customdata"] == point["x"] == 2003


def test_a_re_render_keeps_the_zoom_and_samples_it_from_the_new_data(app: Page):
    relayout(app, "big", {"xaxis.range": [40_000.5, 40_100.5]})
    wait_for_x(app, "big", 40_000, 40_101)

    app.click("#rerender")

    y0 = f"{gd('big')}._fullData[0].y[0]"
    app.wait_for_function(f"() => {y0} > 999 && {gd('big')}._fullData[0].x.length === 102")
    assert drawn(app, "big", 0, "x")[0] == 40_000
    assert app.evaluate(f"() => {gd('big')}._fullLayout.xaxis.range") == [40_000.5, 40_100.5]


def test_a_burst_of_view_changes_sends_one_report_and_draws_the_last(app: Page):
    """A pan of ten steps 30 ms apart, each a view of its own, as a drag produces them."""
    app.evaluate(
        f"""async () => {{
            for (let i = 0; i < 10; i++) {{
                Plotly.relayout({gd("big")}, {{"xaxis.range": [1000 * i + 0.5, 1000 * i + 20.5]}});
                await new Promise((done) => setTimeout(done, 30));
            }}
        }}"""
    )

    wait_for_x(app, "big", 9000, 9021)
    expect(app.locator("#reports")).to_have_text("reports 1")
    app.wait_for_timeout(300)
    assert reports(app) == 1


def inject(page: Page, revision_shift: int, seq_shift: int) -> None:
    """Hand the graph an answer drawing trace 0 as [0, 1], about another revision or view."""
    page.evaluate(
        f"""() => {{
            const gd = {gd("big")};
            const traces = {{0: {{attributes: {{x: [0, 1], y: [0, 1]}}, index_map: [0, 1]}}}};
            const args = [gd._shinyPlotlyResample.revision + {revision_shift},
                          gd._shinyPlotlyView.seq + {seq_shift}, traces];
            Shiny.shinyapp.dispatchMessage(JSON.stringify({{custom: {{"shiny-plotly": {{
                id: "big", method: "resample", args: JSON.stringify(args)}}}}}}));
        }}"""
    )


def test_an_answer_about_an_earlier_view_or_render_is_not_drawn(app: Page):
    before = drawn(app, "big", 0, "x")

    inject(app, 0, -1)
    inject(app, -1, 0)
    app.wait_for_timeout(200)
    assert drawn(app, "big", 0, "x") == before

    inject(app, 0, 0)  # the same answer, about the view on screen, is drawn
    app.wait_for_function(f"() => {gd('big')}._fullData[0].x.length === 2")
