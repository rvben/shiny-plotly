"""The streaming example, served as is: points flow in without a re-render and pause."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

GD = "document.querySelector('#stream .plotly-graph-div')"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/stream/")
    expect(page.locator("#stream svg.main-svg").first).to_be_visible()
    yield page
    assert errors == []


def trace_len(page: Page) -> int:
    return page.evaluate(f"{GD}.data[0].x.length")


def test_points_accumulate_through_extend_traces_and_the_switch_pauses(app: Page):
    # Ticks arrive every 250 ms; the first ones were sent before the empty figure was
    # drawn and must have been held and applied, not lost.
    app.wait_for_function(
        f"() => {{ const gd = {GD}; return gd && gd.data && gd.data[0].x.length >= 3; }}"
    )
    assert app.evaluate(f"{GD}.data[1].x.length") >= 3, "both traces stream"

    app.locator("#run").click()  # the switch: pause

    app.wait_for_timeout(300)  # a tick already in flight may still land
    paused_at = trace_len(app)
    app.wait_for_timeout(900)

    assert trace_len(app) == paused_at, "no ticks after the pause settled"
