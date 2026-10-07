"""The explicit x0/dx date-axis recipe keeps hover, gaps, zoom and point maps."""

import json
from collections.abc import Iterator

import numpy as np
import pytest
from playwright.sync_api import Page

from .apps import DATE_COUNT, DATE_SPIKE

pytestmark = pytest.mark.browser


def state(page: Page, output: str) -> dict:
    return page.evaluate(
        """id => {
            const gd = document.getElementById(id+'-plotly');
            const trace = gd._fullData[0];
            return {x:Array.from(trace._x),
                y:Array.from(trace.y, y=>Number.isFinite(y) ? y : null),
                indices:Array.from(trace.customdata), range:gd.layout.xaxis.range,
                ticks:Array.from(gd.querySelectorAll('.xtick text'), n=>n.textContent),
                type:gd._fullLayout.xaxis.type,
                visible:(() => {
                    const axis=gd._fullLayout.xaxis;
                    const [lo,hi]=axis.range.map(value=>axis.r2c(value));
                    return Array.from(trace.customdata).filter((_,i)=>
                        trace._x[i]>=lo && trace._x[i]<=hi);
                })()};
        }""",
        output,
    )


@pytest.fixture(params=["UTC", "Europe/Amsterdam", "America/New_York"])
def timezone(page: Page, request: pytest.FixtureRequest) -> Iterator[str]:
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setTimezoneOverride", {"timezoneId": request.param})
    try:
        yield request.param
    finally:
        cdp.detach()


def test_step_dates_match_native_dates_and_keep_zoom_events_and_gaps(
    page: Page, server_url: str, errors: list[str], timezone: str
):
    page.route("**/favicon.ico", lambda route: route.fulfill(status=204))
    page.goto(server_url + "/date-steps/")
    assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == timezone
    for output in ("native", "steps"):
        page.wait_for_function(f"document.getElementById('{output}-plotly')?._shinyPlotlyDrawn")
    native, steps = state(page, "native"), state(page, "steps")
    assert native["indices"] == steps["indices"]
    assert native["y"] == steps["y"] and None in steps["y"]
    np.testing.assert_allclose(native["x"], steps["x"], rtol=0, atol=0.001)
    assert native["ticks"] == steps["ticks"]
    assert native["type"] == steps["type"] == "date"

    hovers = []
    for output in ("native", "steps"):
        page.locator(f"#{output}").scroll_into_view_if_needed()
        point = page.evaluate(
            """id => {
                const gd=document.getElementById(id+'-plotly'), trace=gd._fullData[0];
                const layout=gd._fullLayout, box=gd.getBoundingClientRect();
                return {x:box.x+layout.xaxis._offset+layout.xaxis.c2p(trace._x[0]),
                    y:box.y+layout.yaxis._offset+layout.yaxis.c2p(trace.y[0])};
            }""",
            output,
        )
        page.mouse.move(point["x"], point["y"])
        page.wait_for_function(
            f"document.getElementById('{output}-plotly').querySelector('.hovertext')",
            timeout=5000,
        )
        hovers.append(page.locator(f"#{output} .hovertext").text_content())
    assert hovers[0] == hovers[1]
    assert "2026-03-28 00:00:00.123" in hovers[1]

    # Zoom into a small window around the spike with date-string ranges.
    span = ["2026-03-31 00:00:00", "2026-03-31 00:05:00"]
    for output in ("native", "steps"):
        page.evaluate(
            """args => Plotly.relayout(document.getElementById(args[0]+'-plotly'),
                {'xaxis.range':args[1]})""",
            [output, span],
        )
        assert state(page, output)["range"] == span
    native, steps = state(page, "native"), state(page, "steps")
    assert native["indices"] == steps["indices"]
    assert native["ticks"] == steps["ticks"]
    assert native["visible"] == steps["visible"] == list(range(DATE_SPIKE - 1, DATE_SPIKE + 4))
    assert native["y"] == steps["y"]

    # A real pointer click on the spike keeps its full-series identity.
    page.locator("#steps").scroll_into_view_if_needed()
    point = page.evaluate(
        """index => {
            const gd = document.getElementById('steps-plotly');
            const trace=gd._fullData[0], i=Array.from(trace.customdata).indexOf(index);
            const layout=gd._fullLayout, box=gd.getBoundingClientRect();
            return {x:box.x+layout.xaxis._offset+layout.xaxis.c2p(trace._x[i]),
                y:box.y+layout.yaxis._offset+layout.yaxis.d2p(trace.y[i])};
        }""",
        DATE_SPIKE,
    )
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("document.getElementById('steps_click_out').textContent !== '-'")
    clicked = json.loads(page.locator("#steps_click_out").inner_text())
    assert clicked["pointNumber"] == clicked["customdata"] == DATE_SPIKE
    assert str(clicked["x"]).startswith("2026-03-31 00:01:00.123")

    page.click("#shift")
    for output in ("native", "steps"):
        page.wait_for_function(
            f"document.getElementById('{output}-plotly')._fullData[0].y["
            f"Array.from(document.getElementById('{output}-plotly')._fullData[0].customdata)"
            f".indexOf({DATE_SPIKE})] === 51"
        )
        assert state(page, output)["range"] == span
    assert errors == []


@pytest.mark.filterwarnings(
    r"ignore:render_plotly\(resample=200\) sends these traces whole.*numeric date x:UserWarning"
)
def test_numeric_date_fallback_preserves_the_visible_points(
    page: Page, server_url: str, errors: list[str], timezone: str
):
    page.route("**/favicon.ico", lambda route: route.fulfill(status=204))
    page.goto(server_url + "/numeric-date/")
    assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == timezone
    page.wait_for_function("document.getElementById('numeric-plotly')?._shinyPlotlyDrawn")
    result = page.evaluate(
        """async index => {
        const gd=document.getElementById('numeric-plotly'), trace=gd._fullData[0];
        const offset=new Date(trace.x[0]).getTimezoneOffset()*60000;
        const shifted=trace._x[0]-trace.x[0];
        const range=[trace._x[index]-30000,trace._x[index]+30000]
            .map(x=>gd._fullLayout.xaxis.c2r(x));
        await Plotly.relayout(gd, {'xaxis.range':range});
        return {length:trace.y.length, sampled:gd._shinyPlotlyResample,
            shifted, offset, range};
    }""",
        DATE_SPIKE,
    )
    assert result["length"] == DATE_COUNT and result["sampled"] is None
    # This positive control reproduces the offset that made server sampling wrong.
    assert abs(result["shifted"] + result["offset"]) < 0.1
    assert state(page, "numeric")["visible"] == [DATE_SPIKE]
    point = page.evaluate(
        """index => {
        const gd=document.getElementById('numeric-plotly'), layout=gd._fullLayout;
        const trace=gd._fullData[0], box=gd.getBoundingClientRect();
        return {x:box.x+layout.xaxis._offset+layout.xaxis.c2p(trace._x[index]),
            y:box.y+layout.yaxis._offset+layout.yaxis.c2p(trace.y[index])};
    }""",
        DATE_SPIKE,
    )
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("document.getElementById('numeric_click_out').textContent !== '-'")
    clicked = json.loads(page.locator("#numeric_click_out").inner_text())
    assert clicked["pointNumber"] == clicked["customdata"] == DATE_SPIKE
    assert errors == []
