"""Latest waiting snapshots, update ownership and flush through the real output binding."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from .test_defer_offscreen import HOLD_IDLE, HOLD_REACT, release_draw, set_n, start_flush
from .test_update import gd, trace_y, value, wait_for

pytestmark = pytest.mark.browser


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.add_init_script(HOLD_IDLE)
    page.goto(server_url + "/coalesce/")
    for name in ("near", "far", "far_next", "far_plain"):
        expect(page.locator(f"#{name} svg.main-svg").first).to_be_attached()
        wait_for(page, trace_y(name, 0), [1, 2, 3])
    # Exercise coalescing independently of the offscreen policy on the near output.
    page.evaluate("document.getElementById('near').removeAttribute('data-shiny-plotly-defer')")
    yield page
    assert errors == []


def queue(page: Page, figures: list[int], *, update: bool = False) -> None:
    page.evaluate(
        """({figures, update}) => {
        const el = document.getElementById('near');
        const binding = jQuery(el).data('shiny-output-binding').binding;
        for (const n of figures) {
            binding.renderValue(el, {figure: JSON.stringify({
                data: [{type: 'bar', y: [n]}], layout: {uirevision: 'keep'}
            }), config: {responsive: true}});
            if (update) Shiny.shinyapp.dispatchMessage(JSON.stringify({custom: {
                'shiny-plotly': {id: 'near', method: 'restyle',
                    args: JSON.stringify([{y: [[n * 10]]}, [0]])}
            }}));
        }
    }""",
        {"figures": figures, "update": update},
    )


def test_replaces_waiting_figures_and_their_updates(app: Page):
    app.evaluate(HOLD_REACT)
    queue(app, [4, 5, 6], update=True)
    app.wait_for_function("draws.length === 1")
    release_draw(app, 0)
    app.evaluate("() => shinyPlotly.flush()")
    assert value(app, trace_y("near", 0)) == [60]
    assert app.evaluate("draws.length") == 1


def test_serializes_active_draw_updates_and_latest_replacement(app: Page):
    app.evaluate(HOLD_REACT)
    queue(app, [4], update=True)
    app.wait_for_function("draws.length === 1")
    queue(app, [5, 6], update=True)
    start_flush(app)
    assert not app.evaluate("flushed")
    assert app.evaluate("draws.length") == 1
    release_draw(app, 0)
    app.wait_for_function("draws.length === 2")
    assert value(app, trace_y("near", 0)) == [40]
    assert not app.evaluate("flushed")
    release_draw(app, 1)
    app.wait_for_function("flushed")
    assert value(app, trace_y("near", 0)) == [60]


def test_real_server_snapshots_arrive_while_draw_is_active(app: Page):
    app.evaluate("() => { window.unheldReact = Plotly.react; }")
    app.evaluate(HOLD_REACT)
    app.evaluate("""() => {
        const held = Plotly.react;
        Plotly.react = function (gd) {
            return (gd.id === 'far_plain-plotly' ? unheldReact : held).apply(this, arguments);
        };
    }""")
    set_n(app, 4)
    app.wait_for_function("draws.length === 1")
    set_n(app, 5)
    set_n(app, 6)
    start_flush(app)
    release_draw(app, 0)
    # Flush also promotes the two offscreen outputs. Each retains snapshot 6.
    app.wait_for_function("draws.length === 4")
    for index in (1, 2, 3):
        release_draw(app, index)
    app.wait_for_function("flushed")
    for name in ("near", "far", "far_next"):
        assert value(app, trace_y(name, 0)) == list(range(1, 7))
    assert app.evaluate("draws.filter(d => d.id === 'near-plotly').length") == 2


def test_zoom_and_graph_identity_survive_replacement(app: Page):
    queue(app, [4])
    app.evaluate("() => shinyPlotly.flush()")
    app.evaluate(f"() => Plotly.relayout({gd('near')}, {{'xaxis.range': [-0.1, 0.1]}})")
    app.evaluate(f"window.originalGraph = {gd('near')}")
    queue(app, [5, 6])
    app.evaluate("() => shinyPlotly.flush()")
    assert app.evaluate(f"{gd('near')} === originalGraph")
    assert value(app, f"{gd('near')}._fullLayout.xaxis.range") == [-0.1, 0.1]


@pytest.mark.parametrize("kind", ["empty", "error", "remove"])
def test_cancels_active_and_waiting_work(app: Page, kind: str):
    app.evaluate(HOLD_REACT)
    queue(app, [4])
    app.wait_for_function("draws.length === 1")
    queue(app, [5])
    app.evaluate(
        """kind => {
        const el = document.getElementById('near');
        window.cancelledOutput = el;
        const binding = jQuery(el).data('shiny-output-binding').binding;
        if (kind === 'empty') binding.renderValue(el, null);
        else if (kind === 'error') binding.renderError(el, {message: 'server failed'});
        else el.remove();
    }""",
        kind,
    )
    app.wait_for_function("cancelledOutput._shinyPlotlyDeferred.active === null")
    app.evaluate("() => shinyPlotly.flush()")
    expect(app.locator("#near .plotly-graph-div")).to_have_count(0)
    app.evaluate("draws[0].reject(new Error('cancelled draw'))")
    if kind == "remove":
        assert app.evaluate("draws.length") == 1
        return
    app.evaluate("""() => {
        const el = document.getElementById('near');
        jQuery(el).data('shiny-output-binding').binding.clearError(el);
    }""")
    queue(app, [6])
    app.evaluate("() => shinyPlotly.flush()")
    assert value(app, trace_y("near", 0)) == [6]


def test_flush_reports_failure_and_new_figure_recovers(
    page: Page, server_url: str, errors: list[str]
):
    page.goto(server_url + "/coalesce/")
    expect(page.locator("#near svg.main-svg").first).to_be_attached()
    wait_for(page, trace_y("near", 0), [1, 2, 3])
    page.evaluate(HOLD_REACT)
    queue(page, [4])
    page.wait_for_function("draws.length === 1")
    page.evaluate("draws[0].reject(new Error('coalesced draw failed'))")
    start_flush(page)
    page.wait_for_function("flushError === 'coalesced draw failed'")
    assert len(errors) == 1 and "coalesced draw failed" in errors[0]
    errors.clear()
    queue(page, [5])
    page.wait_for_function("draws.length === 2")
    release_draw(page, 1)
    page.evaluate("() => shinyPlotly.flush()")
    assert value(page, trace_y("near", 0)) == [5]
    assert errors == []


def test_ready_queue_does_not_depend_on_animation_frames(app: Page):
    app.evaluate(HOLD_REACT)
    app.evaluate("() => { window.requestAnimationFrame = () => 1; }")
    queue(app, [4, 5])
    app.wait_for_function("draws.length === 1", polling=20)
    app.evaluate("""() => {
        const el = document.getElementById('near');
        jQuery(el).data('shiny-output-binding').binding.renderValue(el, null);
        draws[0].reject(new Error('cancelled draw'));
    }""")
    app.evaluate("() => shinyPlotly.flush()")
