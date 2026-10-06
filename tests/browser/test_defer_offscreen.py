"""Deferred redraws, including updates and flushes arriving during a real Plotly draw."""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from .test_update import gd, trace_y, value, wait_for

pytestmark = pytest.mark.browser
STALE = re.compile(r"\bshiny-plotly-stale\b")
HOLD_IDLE = """
window.idleCallbacks = [];
window.requestIdleCallback = callback => { window.idleCallbacks.push(callback); return 1; };
"""
# Delay the real react until explicitly released, not just its returned promise. This
# exposes stale data, serialization mistakes and messages sent while drawing is active.
HOLD_REACT = """() => {
    const react = Plotly.react;
    window.draws = [];
    Plotly.react = function (gd, figure) {
        return new Promise((resolve, reject) => {
            window.draws.push({id: gd.id,
                release: () => react.call(Plotly, gd, figure).then(resolve, reject), reject});
        });
    };
}"""


@pytest.fixture
def app(page: Page, server_url: str, errors: list[str]) -> Iterator[Page]:
    page.set_viewport_size({"width": 1000, "height": 700})
    page.add_init_script(HOLD_IDLE)
    page.goto(server_url + "/defer/")
    for output_id in ("near", "far", "far_next", "far_plain"):
        expect(page.locator(f"#{output_id} svg.main-svg").first).to_be_attached()
        wait_for(page, trace_y(output_id, 0), [1, 2, 3])
    page.evaluate("""() => {
        window.reacts = {};
        const react = Plotly.react;
        Plotly.react = function (gd) {
            window.reacts[gd.id] = (window.reacts[gd.id] || 0) + 1;
            return react.apply(this, arguments);
        };
    }""")
    yield page
    assert errors == []


def set_n(page: Page, n: int) -> None:
    page.evaluate("n => Shiny.setInputValue('n', n)", n)
    wait_for(page, trace_y("far_plain", 0), list(range(1, n + 1)))


def run_idle(page: Page) -> bool:
    return page.evaluate("""() => {
        const callback = window.idleCallbacks.shift();
        if (!callback) return false;
        callback({didTimeout: false, timeRemaining: () => 50});
        return true;
    }""")


def scroll_to_far(page: Page) -> None:
    page.locator("#far").scroll_into_view_if_needed()


def start_flush(page: Page) -> None:
    page.evaluate("""() => {
        window.flushed = false;
        window.flushError = null;
        window.shinyPlotly.flush().then(
            () => { window.flushed = true; },
            err => { window.flushError = err.message; }
        );
    }""")


def release_draw(page: Page, index: int) -> None:
    page.evaluate("i => window.draws[i].release()", index)


def send_update(page: Page, y: list[int]) -> None:
    # Deliver through the actual registered Shiny custom-message handler.
    page.evaluate(
        """y => Shiny.shinyapp.dispatchMessage(JSON.stringify({custom: {'shiny-plotly': {
        id: 'far', method: 'restyle', args: JSON.stringify([{y: [y]}, [0]])
    }}}))""",
        y,
    )


def test_offscreen_waits_while_onscreen_and_plain_outputs_redraw(app: Page):
    set_n(app, 4)
    wait_for(app, trace_y("near", 0), [1, 2, 3, 4])
    assert value(app, trace_y("far", 0)) == [1, 2, 3]
    expect(app.locator("#far")).to_have_class(STALE)
    expect(app.locator("#near")).not_to_have_class(STALE)
    assert app.evaluate("window.reacts['far-plotly'] || 0") == 0
    scroll_to_far(app)
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4])
    expect(app.locator("#far")).not_to_have_class(STALE)


def test_only_latest_waiting_figure_and_its_updates_survive(app: Page):
    set_n(app, 4)
    app.click("#tick")
    wait_for(app, trace_y("far_plain", 0), [1, 2, 3, 4, 99])
    set_n(app, 5)
    app.click("#tick")
    wait_for(app, trace_y("far_plain", 0), [1, 2, 3, 4, 5, 99])
    assert value(app, trace_y("far", 0)) == [1, 2, 3]
    app.evaluate("() => window.shinyPlotly.flush()")
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4, 5, 99])
    assert app.evaluate("window.reacts['far-plotly']") == 1


def test_idle_draws_one_output_at_a_time(app: Page):
    set_n(app, 4)
    assert run_idle(app)
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4])
    assert value(app, trace_y("far_next", 0)) == [1, 2, 3]
    app.wait_for_function("window.idleCallbacks.length > 0")
    assert run_idle(app)
    wait_for(app, trace_y("far_next", 0), [1, 2, 3, 4])
    assert not run_idle(app)


@pytest.mark.parametrize("event", ["pointerenter", "focusin", "beforeprint"])
def test_interaction_and_native_print_start_refreshing(app: Page, event: str):
    set_n(app, 4)
    if event == "beforeprint":
        app.evaluate("window.dispatchEvent(new Event('beforeprint'))")
    else:
        app.locator("#far").dispatch_event(event)
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4])


@pytest.mark.parametrize("kind", ["hide", "fail"])
def test_empty_and_error_cancel_waiting_figure(app: Page, kind: str):
    set_n(app, 4)
    app.evaluate("kind => Shiny.setInputValue(kind, true)", kind)
    expect(app.locator("#far .plotly-graph-div")).to_have_count(0)
    expect(app.locator("#far")).not_to_have_class(STALE)
    app.evaluate("kind => Shiny.setInputValue(kind, false)", kind)
    expect(app.locator("#far svg.main-svg").first).to_be_attached()
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4])


def test_theme_and_size_changes_catch_up_after_waiting(app: Page):
    set_n(app, 4)
    app.evaluate("document.getElementById('far').style.width = '400px'")
    app.locator("#mode button").first.click()
    app.wait_for_function("document.documentElement.dataset.bsTheme === 'dark'")
    app.evaluate("() => window.shinyPlotly.flush()")
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4])
    wait_for(app, f"{gd('far')}._fullLayout.width", 400)
    wait_for(app, f"{gd('far')}._fullLayout.font.color", "#f2f5fa")


def test_stale_style_is_delayed_and_app_overridable(app: Page):
    set_n(app, 4)
    assert float(app.locator("#far").evaluate("el => getComputedStyle(el).opacity")) > 0.3
    expect(app.locator("#far")).to_have_css("opacity", "0.3")
    app.add_style_tag(content=".shiny-plotly-stale {opacity: 0.8; transition: none;}")
    expect(app.locator("#far")).to_have_css("opacity", "0.8")
    app.evaluate("() => window.shinyPlotly.flush()")
    expect(app.locator("#far")).to_have_css("opacity", "1")


@pytest.mark.parametrize("trigger", ["idle", "pointerenter", "focusin", "scroll"])
def test_flush_waits_for_a_draw_already_started(app: Page, trigger: str):
    set_n(app, 4)
    app.evaluate(HOLD_REACT)
    if trigger == "idle":
        run_idle(app)
    elif trigger == "scroll":
        scroll_to_far(app)
    else:
        app.locator("#far").dispatch_event(trigger)
    app.wait_for_function("window.draws.length > 0")
    start_flush(app)
    app.wait_for_function("window.draws.length === 2")
    assert app.evaluate("window.flushed") is False
    assert value(app, trace_y("far", 0)) == [1, 2, 3]
    expect(app.locator("#far")).to_have_class(STALE)
    release_draw(app, 0)
    assert app.evaluate("window.flushed") is False
    release_draw(app, 1)
    app.wait_for_function("window.flushed")
    assert value(app, trace_y("far", 0)) == [1, 2, 3, 4]
    expect(app.locator("#far")).not_to_have_class(STALE)


def test_two_concurrent_flushes_wait_for_the_same_draws(app: Page):
    set_n(app, 4)
    app.evaluate(HOLD_REACT)
    app.evaluate("""() => {
        window.flushCount = 0;
        for (let i = 0; i < 2; i++) {
            window.shinyPlotly.flush().then(() => window.flushCount++);
        }
    }""")
    app.wait_for_function("window.draws.length === 2")
    assert app.evaluate("window.flushCount") == 0
    release_draw(app, 0)
    release_draw(app, 1)
    app.wait_for_function("window.flushCount === 2")
    assert app.evaluate("window.draws.length") == 2


def test_newer_figure_keeps_its_updates_during_an_active_draw(app: Page):
    set_n(app, 4)
    # Hold only far, so the control and near can acknowledge newer server values.
    app.evaluate("""() => {
        const react = Plotly.react;
        window.draws = [];
        Plotly.react = function (gd, figure) {
            if (gd.id !== 'far-plotly') return react.apply(this, arguments);
            return new Promise((resolve, reject) => window.draws.push({
                release: () => react.call(Plotly, gd, figure).then(resolve, reject)
            }));
        };
    }""")
    run_idle(app)
    app.wait_for_function("window.draws.length === 1")
    send_update(app, [40])
    set_n(app, 5)
    send_update(app, [50])
    start_flush(app)
    assert app.evaluate("window.draws.length") == 1, "same graph draws serially"
    release_draw(app, 0)
    app.wait_for_function("window.draws.length === 2")
    release_draw(app, 1)
    app.wait_for_function("window.flushed")
    assert value(app, trace_y("far", 0)) == [50]


def test_new_updates_do_not_overtake_updates_queued_before_drawing(app: Page):
    set_n(app, 4)
    send_update(app, [40])
    app.evaluate(HOLD_REACT)
    run_idle(app)
    app.wait_for_function("window.draws.length === 1")
    send_update(app, [41])
    release_draw(app, 0)
    wait_for(app, trace_y("far", 0), [41])


def test_idle_drain_does_not_overlap_when_new_values_arrive(app: Page):
    set_n(app, 4)
    app.evaluate(HOLD_REACT)
    run_idle(app)
    app.wait_for_function("window.draws.length === 1")
    # Enqueue a replacement directly through the actual binding without blocking Shiny's
    # message dispatcher on the deliberately held near/control graphs.
    app.evaluate("""() => {
        const el = document.getElementById('far');
        const binding = $(el).data('shiny-output-binding').binding;
        binding.renderValue(el, {figure: JSON.stringify({data: [{y: [5]}]})});
    }""")
    assert not run_idle(app), "no second idle callback while an idle draw is active"
    release_draw(app, 0)
    app.wait_for_function("window.idleCallbacks.length > 0")
    run_idle(app)
    app.wait_for_function("window.draws.length === 2")
    release_draw(app, 1)


def test_flush_rejects_drawing_failures_and_recovers_on_a_new_value(app: Page, errors: list[str]):
    set_n(app, 4)
    app.evaluate(HOLD_REACT)
    start_flush(app)
    app.wait_for_function("window.draws.length === 2")
    app.evaluate("window.draws[0].reject(new Error('intentional draw failure'))")
    app.wait_for_function("window.flushError === 'intentional draw failure'")
    expect(app.locator("#far")).to_have_class(STALE)
    release_draw(app, 1)
    # Replace through the binding; clear the failure only when a fresh value is submitted.
    app.evaluate("""() => {
        const el = document.getElementById('far');
        $(el).data('shiny-output-binding').binding.renderValue(el, {
            figure: JSON.stringify({data: [{y: [5]}]})
        });
    }""")
    start_flush(app)
    app.wait_for_function("window.draws.length === 3")
    release_draw(app, 2)
    app.wait_for_function("window.flushed")
    assert value(app, trace_y("far", 0)) == [5]
    assert errors == []


def test_removing_a_waiting_output_cancels_its_draw(app: Page):
    set_n(app, 4)
    app.evaluate("document.getElementById('far').remove()")
    app.evaluate("() => window.shinyPlotly.flush()")
    assert app.evaluate("window.reacts['far-plotly'] || 0") == 0


def test_timer_fallback_draws_waiting_figures(page: Page, server_url: str, errors: list[str]):
    page.set_viewport_size({"width": 1000, "height": 700})
    page.add_init_script("delete window.requestIdleCallback;")
    page.goto(server_url + "/defer/")
    expect(page.locator("#far svg.main-svg").first).to_be_attached()
    wait_for(page, trace_y("far", 0), [1, 2, 3])
    page.evaluate("Shiny.setInputValue('n', 4)")
    wait_for(page, trace_y("far", 0), [1, 2, 3, 4])
    wait_for(page, trace_y("far_next", 0), [1, 2, 3, 4])
    assert errors == []


@pytest.mark.parametrize(
    ("top", "left", "deferred"),
    [
        (899, 0, False),
        (901, 0, True),
        (-349, 0, False),
        (-351, 0, True),
        (100, 1199, False),
        (100, 1201, True),
    ],
)
def test_viewport_margin_in_both_directions(app: Page, top: int, left: int, deferred: bool):
    app.evaluate(
        """([top, left]) => {
        Object.assign(document.getElementById('far').style, {
            position: 'fixed', top: top + 'px', left: left + 'px', width: '400px'
        });
    }""",
        [top, left],
    )
    set_n(app, 4)
    if deferred:
        assert value(app, trace_y("far", 0)) == [1, 2, 3]
        expect(app.locator("#far")).to_have_class(STALE)
    else:
        wait_for(app, trace_y("far", 0), [1, 2, 3, 4])
        expect(app.locator("#far")).not_to_have_class(STALE)


def test_scroll_promotion_uses_viewport_bounds_inside_a_clipping_container(app: Page):
    app.evaluate("""() => {
        const far = document.getElementById('far');
        const clip = document.createElement('div');
        clip.id = 'clip';
        Object.assign(clip.style, {overflow: 'hidden', height: '100px', width: '400px'});
        far.before(clip);
        clip.appendChild(far);
        far.style.marginTop = '150px';
    }""")
    set_n(app, 4)
    app.evaluate("""() => {
        Object.assign(document.getElementById('clip').style, {position: 'fixed', top: '100px'});
        window.dispatchEvent(new Event('scroll'));
    }""")
    wait_for(app, trace_y("far", 0), [1, 2, 3, 4])


def test_flush_waits_for_updates_arriving_while_the_queue_is_draining(app: Page):
    set_n(app, 4)
    send_update(app, [40])
    app.evaluate("""() => {
        const restyle = Plotly.restyle;
        window.updates = [];
        Plotly.restyle = function (gd) {
            const args = arguments;
            if (gd.id !== 'far-plotly') return restyle.apply(this, args);
            return new Promise((resolve, reject) => window.updates.push({
                release: () => restyle.apply(Plotly, args).then(resolve, reject)
            }));
        };
    }""")
    run_idle(app)
    app.wait_for_function("window.updates.length === 1")
    start_flush(app)
    send_update(app, [41])
    assert app.evaluate("window.flushed") is False
    app.evaluate("() => window.updates[0].release()")
    app.wait_for_function("window.updates.length === 2")
    assert app.evaluate("window.flushed") is False
    app.evaluate("() => window.updates[1].release()")
    app.wait_for_function("window.flushed")
    assert value(app, trace_y("far", 0)) == [41]


@pytest.mark.parametrize("kind", ["empty", "error"])
def test_cancelling_an_active_draw_drops_its_updates_and_allows_a_fresh_first_draw(
    app: Page, kind: str
):
    set_n(app, 4)
    app.evaluate(HOLD_REACT)
    run_idle(app)
    app.wait_for_function("window.draws.length === 1")
    send_update(app, [40])
    app.evaluate(
        """kind => {
        const el = document.getElementById('far');
        const binding = $(el).data('shiny-output-binding').binding;
        if (kind === 'empty') binding.renderValue(el, null);
        else binding.renderError(el, {message: 'intentional server error'});
    }""",
        kind,
    )
    expect(app.locator("#far .plotly-graph-div")).to_have_count(0)
    expect(app.locator("#far")).not_to_have_class(STALE)
    if kind == "error":
        expect(app.locator("#far")).to_have_text("intentional server error")
    app.evaluate("""() => {
        const el = document.getElementById('far');
        const binding = $(el).data('shiny-output-binding').binding;
        binding.clearError(el);
        window.freshDraw = binding.renderValue(el, {figure: JSON.stringify({data: [{y: [5]}]})});
    }""")
    app.evaluate("() => window.freshDraw")
    assert value(app, trace_y("far", 0)) == [5], "fresh first draw does not await cancelled work"
    release_draw(app, 0)
    assert value(app, trace_y("far", 0)) == [5]


def test_deferred_resampled_figure_keeps_zoom_and_requests_the_new_revision(
    page: Page, server_url: str, errors: list[str]
):
    from .test_resample import drawn, relayout, wait_for_x

    page.add_init_script(HOLD_IDLE)
    page.goto(server_url + "/resample/")
    expect(page.locator("#big svg.main-svg").first).to_be_attached()
    relayout(page, "big", {"xaxis.range": [40_000.5, 40_100.5]})
    wait_for_x(page, "big", 40_000, 40_101)
    page.evaluate("""() => {
        const el = document.getElementById('big');
        el.setAttribute('data-shiny-plotly-defer', '');
        el.style.marginTop = '3000px';
        Shiny.setInputValue('rerender', 1, {priority: 'event'});
    }""")
    expect(page.locator("#big")).to_have_class(STALE)
    assert max(drawn(page, "big", 0, "y")) < 2
    page.evaluate("() => window.shinyPlotly.flush()")
    wait_for_x(page, "big", 40_000, 40_101)
    page.wait_for_function(f"{gd('big')}._fullData[0].y[0] > 999")
    assert drawn(page, "big", 0, "customdata") == list(range(40_000, 40_102))
    assert errors == []


def test_output_removed_during_first_draw_is_purged_when_the_draw_finishes(app: Page):
    app.evaluate("""() => {
        const newPlot = Plotly.newPlot;
        const purge = Plotly.purge;
        window.purgedIds = [];
        Plotly.purge = function (gd) {
            window.purgedIds.push(gd.id);
            return purge.apply(this, arguments);
        };
        Plotly.newPlot = function (gd, figure) {
            return new Promise((resolve, reject) => {
                window.releaseFirst = () => newPlot.call(Plotly, gd, figure).then(resolve, reject);
            });
        };
        const el = document.createElement('div');
        el.id = 'dynamic';
        el.setAttribute('data-shiny-plotly-defer', '');
        document.body.appendChild(el);
        const binding = $('#far').data('shiny-output-binding').binding;
        window.dynamicDraw = binding.renderValue(el, {figure: JSON.stringify({data: [{y: [1]}]})});
    }""")
    app.wait_for_function("typeof window.releaseFirst === 'function'")
    app.evaluate("document.getElementById('dynamic').remove()")
    app.evaluate("() => window.releaseFirst()")
    app.evaluate("() => window.dynamicDraw")
    assert "dynamic-plotly" in app.evaluate("window.purgedIds")


def test_removing_an_output_after_a_failed_first_draw_releases_retained_failure(app: Page, errors):
    app.evaluate("""() => {
        Plotly.newPlot = () => Promise.reject(new Error('intentional first draw failure'));
        window.failedOutput = document.createElement('div');
        window.failedOutput.id = 'failed-first';
        window.failedOutput.setAttribute('data-shiny-plotly-defer', '');
        document.body.appendChild(window.failedOutput);
        const binding = $('#far').data('shiny-output-binding').binding;
        window.firstFailure = binding.renderValue(window.failedOutput, {
            figure: JSON.stringify({data: [{y: [1]}]})
        });
    }""")
    app.evaluate("() => window.firstFailure")
    assert app.evaluate("() => shinyPlotly.flush().catch(err => err.message)") == (
        "intentional first draw failure"
    )
    assert len(errors) == 1 and "intentional first draw failure" in errors[0]
    errors.clear()
    app.evaluate("window.failedOutput.remove()")
    app.wait_for_function("window.failedOutput._shinyPlotlyDeferred.error === null")
    app.evaluate("() => window.shinyPlotly.flush()")


def test_failed_predecessor_does_not_reject_a_new_value_or_skip_later_outputs(app: Page, errors):
    set_n(app, 4)
    app.evaluate(HOLD_REACT)
    run_idle(app)
    app.wait_for_function("window.draws.length === 1")
    app.evaluate("""() => {
        Object.assign(document.getElementById('far').style, {position: 'fixed', top: '100px'});
        const figure = y => ({figure: JSON.stringify({data: [{y: [y]}]})});
        window.messageDone = false;
        window.replacementError = null;
        Shiny.shinyapp.dispatchMessage(JSON.stringify({values: {
            far: figure(5), near: figure(6)
        }})).then(() => { window.messageDone = true; },
                  err => { window.replacementError = err.message; });
    }""")
    app.evaluate("window.draws[0].reject(new Error('intentional predecessor failure'))")
    app.wait_for_function("window.draws.length === 2 || window.replacementError !== null")
    assert app.evaluate("window.replacementError") is None
    release_draw(app, 1)
    app.wait_for_function("window.draws.length === 3")
    release_draw(app, 2)
    app.wait_for_function("window.messageDone")
    assert value(app, trace_y("far", 0)) == [5]
    assert value(app, trace_y("near", 0)) == [6]
    assert len(errors) == 1 and "intentional predecessor failure" in errors[0]
    errors.clear()


@pytest.mark.parametrize("deferred", [False, True])
def test_failed_first_draw_recovers_with_all_first_draw_initialization(app: Page, deferred, errors):
    app.evaluate(
        """deferred => {
        const original = Plotly.newPlot;
        const el = document.createElement('div');
        el.id = 'recovery';
        el.style.height = '150px';
        if (deferred) el.setAttribute('data-shiny-plotly-defer', '');
        document.body.appendChild(el);
        const binding = $('#far').data('shiny-output-binding').binding;
        Plotly.newPlot = () => Promise.reject(new Error('intentional first failure'));
        window.recovery = binding.renderValue(el, {
            figure: JSON.stringify({data: [{y: [1]}]})
        }).catch(err => err.message).then(async message => {
            if (deferred) message = await shinyPlotly.flush().catch(err => err.message);
            window.firstError = message;
            Plotly.newPlot = original;
            return binding.renderValue(el, {
                figure: JSON.stringify({data: [{y: [2]}]}),
                events: ['click'], post_script: 'window.recoveredPostScript = true;'
            });
        });
    }""",
        deferred,
    )
    app.evaluate("() => window.recovery")
    assert app.evaluate("window.firstError") == "intentional first failure"
    assert app.evaluate("window.recoveredPostScript") is True
    assert app.evaluate(f"Boolean({gd('recovery')}._shinyPlotlyView)")
    assert app.evaluate(f"{gd('recovery')}._shinyPlotlyDrawn") is True
    assert value(app, trace_y("recovery", 0)) == [2]
    if deferred:
        assert len(errors) == 1 and "intentional first failure" in errors[0]
        errors.clear()


def test_updates_after_failure_are_dropped_instead_of_retained_forever(app: Page):
    app.evaluate("""() => {
        const el = document.getElementById('far');
        Plotly.react = () => Promise.reject(new Error('intentional draw failure'));
        const binding = $(el).data('shiny-output-binding').binding;
        window.failure = binding.renderValue(el, {
            figure: JSON.stringify({data: [{y: [4]}]})
        });
    }""")
    assert app.evaluate("() => shinyPlotly.flush().catch(err => err.message)") == (
        "intentional draw failure"
    )
    for y in ([40], [41], [42]):
        send_update(app, y)
    assert app.evaluate("document.getElementById('far')._shinyPlotlyPending == null")
    assert app.evaluate("() => shinyPlotly.flush().catch(err => err.message)") == (
        "intentional draw failure"
    )


def test_own_draw_failure_preserves_later_outputs_and_remains_observable(app: Page, errors):
    app.evaluate("""() => {
        Object.assign(document.getElementById('far').style, {position: 'fixed', top: '100px'});
        const react = Plotly.react;
        Plotly.react = (gd, figure) => gd.id === 'far-plotly'
            ? Promise.reject(new Error('intentional own failure')) : react(gd, figure);
        const figure = y => ({figure: JSON.stringify({data: [{y: [y]}]})});
        return Shiny.shinyapp.dispatchMessage(JSON.stringify({values: {
            far: figure(5), near: figure(6)
        }}));
    }""")
    assert value(app, trace_y("near", 0)) == [6]
    assert app.evaluate("() => shinyPlotly.flush().catch(err => err.message)") == (
        "intentional own failure"
    )
    assert len(errors) == 1 and "intentional own failure" in errors[0]
    errors.clear()
