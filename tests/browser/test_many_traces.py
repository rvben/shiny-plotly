"""The many-trace example sends one targeted update and preserves user state."""

import math

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser
# The real example has 100 WebGL traces; software rendering with the oldest
# Plotly can take more than Playwright's default 30 seconds to finish a draw.
DRAW_TIMEOUT_MS = 60_000


def watch_updates(page: Page) -> None:
    # Large WebGL redraws on the oldest Plotly can outlast locator assertions.
    # Observe completion of the actual update before inspecting its SVG title.
    page.evaluate("""() => {
        window.completedUpdates = 0;
        const update = Plotly.update;
        Plotly.update = function(...args) {
            const result = update.apply(this, args);
            result.then(() => window.completedUpdates++);
            return result;
        };
    }""")


def test_one_trace_changes_without_rerendering_or_losing_the_zoom(
    page: Page, server_url: str, errors: list[str]
):
    page.goto(server_url + "/many/", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('chart-plotly')?._shinyPlotlyDrawn", timeout=DRAW_TIMEOUT_MS
    )
    watch_updates(page)
    page.evaluate("""async () => {
        const gd = document.getElementById('chart-plotly');
        await Plotly.relayout(gd, {'xaxis.range':[100,200]});
        window.fullDraws = 0; window.updates = 0;
        const react = Plotly.react; const update = Plotly.update;
        Plotly.react = function() { window.fullDraws++; return react.apply(this,arguments); };
        Plotly.update = function() { window.updates++; return update.apply(this,arguments); };
        Shiny.setInputValue('series', 3);
        Shiny.setInputValue('offset', 1);
    }""")
    page.click("#apply", no_wait_after=True)
    page.wait_for_function("completedUpdates === 1", timeout=DRAW_TIMEOUT_MS)
    expect(page.locator("#chart .gtitle")).to_have_text("Changed series 3")
    state = page.evaluate("""() => {
        const gd = document.getElementById('chart-plotly');
        return {changed:gd._fullData[3].y[0], other:gd._fullData[4].y[0],
            range:gd.layout.xaxis.range, traces:gd.data.length,
            full:window.fullDraws, updates:window.updates};
    }""")
    assert math.isclose(state["changed"], 1.3)
    assert math.isclose(state["other"], 0.4)
    assert state["range"] == [100, 200]
    assert state["traces"] == 100
    assert state["full"] == 0
    assert state["updates"] == 1
    assert errors == []


@pytest.mark.parametrize("invalid", ["", "2.5", "-1", "100"])
def test_invalid_series_does_not_disconnect_and_a_valid_update_still_works(
    page: Page, server_url: str, errors: list[str], invalid: str
):
    page.goto(server_url + "/many/", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('chart-plotly')?._shinyPlotlyDrawn", timeout=DRAW_TIMEOUT_MS
    )
    watch_updates(page)
    page.fill("#series", invalid)
    page.click("#apply", no_wait_after=True)
    expect(page.locator(".shiny-notification")).to_contain_text("Choose a whole-number series")
    page.fill("#series", "3")
    page.click("#apply", no_wait_after=True)
    page.wait_for_function("completedUpdates === 1", timeout=DRAW_TIMEOUT_MS)
    expect(page.locator("#chart .gtitle")).to_have_text("Changed series 3")
    assert errors == []
