"""One resize tracker per graph, with Plotly's native handler as the fallback."""

import plotly.graph_objects as go
import pytest
from playwright.sync_api import Page, expect

from shiny_plotly import fig_to_ui

from .test_browser import wait_until_graph_matches_container

pytestmark = pytest.mark.browser


HANDLERS = """(() => {
    window.dashboardResizeHandlers = new Set();
    const add = window.addEventListener, remove = window.removeEventListener;
    window.addEventListener = function(type, fn, ...rest) {
        if (type === 'resize') dashboardResizeHandlers.add(fn);
        return add.call(this, type, fn, ...rest);
    };
    window.removeEventListener = function(type, fn, ...rest) {
        if (type === 'resize') dashboardResizeHandlers.delete(fn);
        return remove.call(this, type, fn, ...rest);
    };
})();"""


@pytest.mark.parametrize("observer", [True, False])
def test_resize_ownership_survives_rerender(
    page: Page, server_url: str, errors: list[str], observer
):
    page.add_init_script(HANDLERS)
    if not observer:
        # Shiny itself requires ResizeObserver. Hide it only during the helper's
        # draw-completion callback, then restore it before other framework work.
        page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
            for (const name of ['newPlot', 'react']) {
                const original = Plotly[name];
                Plotly[name] = function(...args) {
                    const result = original.apply(this, args);
                    result.then(() => {
                        const Observer = window.ResizeObserver;
                        window.ResizeObserver = undefined;
                        queueMicrotask(() => window.ResizeObserver = Observer);
                    });
                    return result;
                };
            }
        });""")
    page.goto(server_url + "/")
    expect(page.locator("#fig .bars .point")).to_have_count(3)
    if not observer:
        page.set_viewport_size({"width": 1150, "height": 800})
    wait_until_graph_matches_container(page, "fig")
    for n in (3, 5):
        if n != 3:
            page.evaluate("n => Shiny.setInputValue('n', n)", n)
            expect(page.locator("#fig .bars .point")).to_have_count(n)
        page.wait_for_function(
            """observer => {
            const gd = document.querySelector('#fig .plotly-graph-div');
            return gd._shinyPlotlyDrawn && dashboardResizeHandlers.has(
                gd._responsiveChartHandler) === !observer;
        }""",
            arg=observer,
        )
        page.set_viewport_size({"width": 1100 if n == 3 else 900, "height": 800})
        wait_until_graph_matches_container(page, "fig")
        if n == 3:
            # A config change clears Plotly's handler. The next server render restores
            # responsive=True; ownership must be correct for the new handler too.
            page.evaluate("""() => {
                const gd = document.querySelector('#fig .plotly-graph-div');
                return Plotly.react(gd, gd.data, gd.layout, {responsive:false});
            }""")
    assert errors == []


def test_fragment_uses_observer_sizing_and_releases_its_graph(
    page: Page, server_url: str, errors: list[str]
):
    page.add_init_script(HANDLERS)
    page.goto(server_url + "/")
    expect(page.locator("#fig .bars .point")).to_have_count(3)
    fragment = fig_to_ui(go.Figure(go.Bar(y=[1, 2, 3])), div_id="fragment", height="200px")
    assert fragment is not None
    page.evaluate(
        """html => {
        const host = document.createElement('div');
        host.id = 'fragment-host'; host.style.width = '400px';
        document.body.appendChild(host);
        jQuery(host).html(html);
    }""",
        fragment.render()["html"],
    )
    page.wait_for_function("""() => {
        const gd = document.getElementById('fragment');
        window.fragmentGraph = gd;
        return gd._fullLayout?.width === 400 &&
            !dashboardResizeHandlers.has(gd._responsiveChartHandler);
    }""")
    page.evaluate("document.getElementById('fragment-host').style.width = '500px'")
    page.wait_for_function("fragmentGraph._fullLayout.width === 500")
    page.evaluate("document.getElementById('fragment-host').remove()")
    page.wait_for_function("fragmentGraph._fullLayout === undefined")
    assert errors == []
