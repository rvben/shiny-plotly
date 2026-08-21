"""Real-browser checks: what the package promises only shows in a rendering engine."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from packaging.version import Version
from playwright.sync_api import Page, expect
from shiny import __version__ as SHINY_VERSION

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.goto(server_url + "/")
    expect(page.locator(f"#fig {SVG}").first).to_be_visible()
    expect(page.locator(f"#fixed {SVG}").first).to_be_visible()
    wait_until_graph_matches_container(page, "fig")
    yield page
    assert errors == []


def wait_until_graph_matches_container(page: Page, output_id: str, timeout: float = 3000):
    page.wait_for_function(
        "id => { const s = document.querySelector('#' + id + ' svg.main-svg');"
        " const r = document.querySelector('#' + id + ' .shiny-plotly').getBoundingClientRect();"
        " return Math.abs(+s.getAttribute('width') - r.width) <= 1"
        " && Math.abs(+s.getAttribute('height') - r.height) <= 1; }",
        arg=output_id,
        timeout=timeout,
    )


def svg_size(page: Page, output_id: str) -> tuple[float, float]:
    return tuple(
        page.evaluate(
            "id => { const s = document.querySelector('#' + id + ' svg.main-svg');"
            " return [+s.getAttribute('width'), +s.getAttribute('height')]; }",
            output_id,
        )
    )


def container_size(page: Page, output_id: str) -> tuple[float, float]:
    return tuple(
        page.evaluate(
            "id => { const r = document.querySelector('#' + id + ' .shiny-plotly')"
            ".getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }",
            output_id,
        )
    )


def test_figures_render_with_bars_and_no_console_errors(app: Page):
    expect(app.locator("#fig .bars .point")).to_have_count(3)
    expect(app.locator("#fixed .bars .point")).to_have_count(3)


def test_fixed_size_options_reach_the_browser(app: Page):
    assert container_size(app, "fixed") == (400, 220)
    assert svg_size(app, "fixed") == (400, 220)


def test_fill_plot_settles_to_the_card_body_size_after_siblings_render(page: Page, server_url):
    """The card shrinks when the sibling output below it renders; no window resize happens."""
    page.goto(server_url + "/")
    expect(page.locator(f"#fixed {SVG}").first).to_be_visible()

    wait_until_graph_matches_container(page, "fig")

    assert 100 < container_size(page, "fig")[1] < 400, "the card body is squeezed below the basis"


def test_rerender_replaces_the_bars(app: Page):
    app.evaluate("Shiny.setInputValue('n', 7)")

    expect(app.locator("#fig .bars .point")).to_have_count(7)
    expect(app.locator("#fixed .bars .point")).to_have_count(7)


def test_rerender_keeps_the_graph_div_and_its_plotly_state(app: Page):
    """Plotly.react redraws in place: same element, same listeners, no teardown."""
    old = app.evaluate_handle("document.querySelector('#fig .plotly-graph-div')")

    app.evaluate("Shiny.setInputValue('n', 5)")
    expect(app.locator("#fig .bars .point")).to_have_count(5)

    assert app.evaluate(
        "gd => gd.isConnected && gd === document.querySelector('#fig .plotly-graph-div')"
        " && gd._fullLayout !== undefined",
        old,
    )


def test_graph_is_purged_once_its_output_leaves_the_document(app: Page):
    """Plotly keeps a window resize listener per graph div; a removed output must release it."""
    gd = app.evaluate_handle("document.querySelector('#fixed .plotly-graph-div')")

    app.evaluate("() => document.getElementById('fixed').remove()")

    app.wait_for_function(
        "gd => !gd.isConnected && gd._fullLayout === undefined"
        " && gd._responsiveChartHandler === undefined",
        arg=gd,
        timeout=3000,
    )


def drag_zoom(page: Page, output_id: str) -> None:
    box = page.locator(f"#{output_id} .nsewdrag").bounding_box()
    assert box is not None
    x0, y0 = box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.2
    x1, y1 = box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.7
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=8)
    page.mouse.up()


def x_range(page: Page, output_id: str) -> list[float] | None:
    return page.evaluate(
        "id => { const gd = document.querySelector('#' + id + ' .plotly-graph-div');"
        " return gd.layout.xaxis && gd.layout.xaxis.autorange === false"
        " ? gd.layout.xaxis.range : null; }",
        output_id,
    )


def test_uirevision_keeps_a_dragged_zoom_across_a_rerender(app: Page):
    drag_zoom(app, "fixed")
    app.wait_for_function(
        "() => document.querySelector('#fixed .plotly-graph-div').layout.xaxis.autorange === false",
        timeout=3000,
    )
    zoomed = x_range(app, "fixed")
    assert zoomed is not None

    app.evaluate("Shiny.setInputValue('n', 9)")
    expect(app.locator("#fixed .bars .point")).to_have_count(9)

    assert x_range(app, "fixed") == zoomed


def test_rerender_resets_the_view_when_the_figure_has_no_uirevision(app: Page):
    drag_zoom(app, "fig")
    app.wait_for_function(
        "() => document.querySelector('#fig .plotly-graph-div').layout.xaxis.autorange === false",
        timeout=3000,
    )

    app.evaluate("Shiny.setInputValue('n', 9)")
    expect(app.locator("#fig .bars .point")).to_have_count(9)

    assert x_range(app, "fig") is None, "a figure without uirevision starts afresh"


def test_graph_follows_its_container_without_a_window_resize(app: Page):
    app.evaluate(
        "() => { const c = document.querySelector('#fixed .shiny-plotly');"
        " c.style.height = '320px'; c.style.width = '500px'; }"
    )

    app.wait_for_function(
        "() => { const s = document.querySelector('#fixed svg.main-svg');"
        " return s.getAttribute('width') === '500' && s.getAttribute('height') === '320'; }",
        timeout=3000,
    )


def test_full_screen_card_grows_the_plot_and_shrinks_it_back(app: Page):
    before = svg_size(app, "fig")

    app.locator("#card").hover()
    app.locator("#card .bslib-full-screen-enter").click()
    app.wait_for_function(
        "h => +document.querySelector('#fig svg.main-svg').getAttribute('height') > h * 1.5",
        arg=before[1],
        timeout=3000,
    )
    assert svg_size(app, "fig") == container_size(app, "fig")

    app.locator(".bslib-full-screen-exit").click()
    wait_until_graph_matches_container(app, "fig")
    assert svg_size(app, "fig") == before


def test_post_script_forwards_plotly_clicks_to_a_shiny_input(app: Page):
    expect(app.locator("#click_info")).to_have_text("clicked nothing")

    app.locator("#fig .bars .point").nth(1).click(force=True)

    expect(app.locator("#click_info")).to_have_text("clicked x1 (1)")


def test_post_script_runs_once_so_a_rerender_does_not_stack_click_handlers(app: Page):
    app.evaluate("Shiny.setInputValue('n', 4)")
    expect(app.locator("#fig .bars .point")).to_have_count(4)
    app.evaluate("Shiny.setInputValue('n', 5)")
    expect(app.locator("#fig .bars .point")).to_have_count(5)

    app.locator("#fig .bars .point").nth(2).click(force=True)

    expect(app.locator("#click_info")).to_have_text("clicked x2 (1)")
    app.wait_for_timeout(300)
    expect(app.locator("#click_info")).to_have_text("clicked x2 (1)")


def test_render_error_is_shown_and_the_graph_returns_after_recovery(app: Page):
    app.evaluate("Shiny.setInputValue('fail', true)")

    expect(app.locator("#fig")).to_have_class(re.compile(r"\bshiny-output-error\b"))
    expect(app.locator("#fig")).to_contain_text("the figure failed on purpose")
    expect(app.locator("#fig .plotly-graph-div")).to_have_count(0)

    app.evaluate("Shiny.setInputValue('fail', false)")

    expect(app.locator("#fig .bars .point")).to_have_count(3)
    expect(app.locator("#fig")).not_to_have_class(re.compile(r"\bshiny-output-error\b"))
    expect(app.locator("#fig")).not_to_contain_text("failed")


def test_none_empties_the_output_and_a_figure_brings_it_back(app: Page):
    app.evaluate("Shiny.setInputValue('hide', true)")

    expect(app.locator("#fig .plotly-graph-div")).to_have_count(0)
    expect(app.locator("#fig")).to_be_empty()

    app.evaluate("Shiny.setInputValue('hide', false)")

    expect(app.locator("#fig .bars .point")).to_have_count(3)


@dataclass(frozen=True)
class HiddenChart:
    """A chart the /hidden page keeps out of sight, and what brings it into view."""

    container: str
    output_id: str
    points: int
    reveal: Callable[[Page], None]


# Shiny suspends an output inside a false ``ui.panel_conditional`` from 1.6.1 on; 1.6.0 and
# older, the declared floor among them, draw that chart at load. Measured by installing each
# release and running this case; every other container here defers at every supported version.
CONDITIONAL_IS_DEFERRED = Version(SHINY_VERSION) >= Version("1.6.1")

HIDDEN_CHARTS = [
    HiddenChart(
        "navset_tab", "second", 3, lambda page: page.get_by_role("tab", name="Second").click()
    ),
    HiddenChart(
        "navset_card_tab", "card", 4, lambda page: page.get_by_role("tab", name="Card two").click()
    ),
    HiddenChart(
        "navset_pill", "pill", 5, lambda page: page.get_by_role("tab", name="Pill two").click()
    ),
    HiddenChart(
        "accordion",
        "folded",
        6,
        lambda page: page.get_by_role("button", name="Folded section").click(),
    ),
    pytest.param(
        HiddenChart(
            "panel_conditional", "conditional", 7, lambda page: page.click("#show_conditional")
        ),
        marks=pytest.mark.skipif(
            not CONDITIONAL_IS_DEFERRED,
            reason=f"shiny {SHINY_VERSION} draws this one at load; 1.6.1 is where it defers",
        ),
    ),
    HiddenChart("navset_hidden", "swapped", 8, lambda page: page.click("#show_swapped")),
]

# The open tab panel, and the chart 3000px down the page, which the browser calls visible even
# though nobody can see it yet. Both are controls: they prove a snapshot of the page sees a
# chart when there is one, so an id missing from it is an output Shiny suspended rather than a
# test that looked too early.
DRAWN_AT_LOAD = sorted(
    ["first-plotly", "below-plotly"] + ([] if CONDITIONAL_IS_DEFERRED else ["conditional-plotly"])
)


def open_the_hidden_page(page: Page, server_url: str) -> None:
    """Load /hidden and wait until every output that is going to render has rendered."""
    page.goto(server_url + "/hidden/")
    expect(page.locator(f"#below {SVG}").first).to_be_visible()
    page.wait_for_function("() => window.shinyIdleCount >= 1")


def test_only_the_charts_the_visitor_can_see_are_drawn_at_load(page: Page, server_url, errors):
    """Shiny suspends a hidden output, the lever the README hands a crowded dashboard."""
    open_the_hidden_page(page, server_url)

    drawn = page.eval_on_selector_all(".plotly-graph-div", "els => els.map(e => e.id).sort()")

    assert drawn == DRAWN_AT_LOAD
    assert errors == []


@pytest.mark.parametrize("case", HIDDEN_CHARTS, ids=lambda case: case.container)
def test_a_hidden_chart_is_drawn_when_its_container_shows_it(
    case: HiddenChart, page: Page, server_url, errors
):
    """Every container that hides a chart defers it, and pays for it when it is opened."""
    open_the_hidden_page(page, server_url)
    assert page.evaluate(f"() => document.getElementById('{case.output_id}-plotly')") is None

    case.reveal(page)

    expect(page.locator(f"#{case.output_id} {SVG}").first).to_be_visible()
    # Its own bar count, so the reveal drew this chart rather than any of the others.
    expect(page.locator(f"#{case.output_id} .bars .point")).to_have_count(case.points)
    assert errors == []


def test_bundle_loads_on_demand_without_a_page_level_plotly_js(page: Page, server_url, errors):
    page.goto(server_url + "/lazy/")

    expect(page.locator(f"#lazy {SVG}").first).to_be_visible()
    expect(page.locator("#lazy .bars .point")).to_have_count(2)
    assert errors == []


def bundle_timing(page: Page) -> dict:
    """Resource Timing entry for plotly.min.js: how the browser actually fetched it."""
    return page.evaluate(
        "() => { const e = performance.getEntriesByType('resource')"
        ".find(e => e.name.endsWith('/plotly.min.js'));"
        " return { transfer: e.transferSize, encoded: e.encodedBodySize,"
        " decoded: e.decodedBodySize }; }"
    )


def test_the_bundle_arrives_compressed_and_immutable_for_the_next_visitor(
    page, browser, server_url
):
    from shiny_plotly import _serve

    page.goto(server_url + "/")  # the first session of the process turns the route on
    expect(page.locator(f"#fig {SVG}").first).to_be_visible()
    assert _serve.bundle().wait(timeout=30)

    visitor = browser.new_context()  # a fresh cache, like a new user
    try:
        first = visitor.new_page()
        with first.expect_response(lambda r: r.url.endswith("/plotly.min.js")) as bundle:
            first.goto(server_url + "/")
        expect(first.locator(f"#fig {SVG}").first).to_be_visible()
        headers = bundle.value.headers
        fresh = bundle_timing(first)

        first.goto(server_url + "/")
        expect(first.locator(f"#fig {SVG}").first).to_be_visible()
        again = bundle_timing(first)
    finally:
        visitor.close()

    assert headers["content-encoding"] in ("br", "gzip")
    assert headers["cache-control"] == "public, max-age=31536000, immutable"
    assert fresh["decoded"] == _serve.bundle().size, "the whole bundle, whichever plotly"
    assert fresh["encoded"] < fresh["decoded"] // 3, "brotli or gzip on the wire"
    assert fresh["transfer"] > fresh["encoded"], "headers plus the compressed body"
    # Chromium would keep a freshly installed bundle for a while on heuristics alone; the
    # header above is what makes this hold for a year, for every install age.
    assert again["transfer"] == 0, "served from the cache without a revalidation"
