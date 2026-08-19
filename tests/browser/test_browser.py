"""Real-browser checks: what the package promises only shows in a rendering engine."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

SVG = "svg.main-svg"


@pytest.fixture
def errors(page: Page) -> Iterator[list[str]]:
    """Console errors and uncaught exceptions raised while a test drives the page."""
    seen: list[str] = []
    page.on("pageerror", lambda err: seen.append(f"pageerror: {err}"))
    page.on("console", lambda msg: seen.append(msg.text) if msg.type == "error" else None)
    yield seen


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


def test_rerender_releases_the_previous_graph(app: Page):
    """Plotly keeps a window resize listener per graph div; a re-render must purge the old one."""
    old = app.evaluate_handle("document.querySelector('#fig .plotly-graph-div')")

    app.evaluate("Shiny.setInputValue('n', 5)")
    expect(app.locator("#fig .bars .point")).to_have_count(5)

    app.wait_for_function(
        "gd => !gd.isConnected && gd._fullLayout === undefined"
        " && gd._responsiveChartHandler === undefined",
        arg=old,
        timeout=3000,
    )


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

    expect(app.locator("#click_info")).to_have_text("clicked x1")


def test_bundle_loads_on_demand_without_a_page_level_plotly_js(page: Page, server_url, errors):
    page.goto(server_url + "/lazy/")

    expect(page.locator(f"#lazy {SVG}").first).to_be_visible()
    expect(page.locator("#lazy .bars .point")).to_have_count(2)
    assert errors == []
