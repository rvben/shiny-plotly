"""Shiny apps exercised by the browser tests."""

import asyncio
import json
from typing import Any

import numpy as np
import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, module, reactive, render, ui

from shiny_plotly import (
    add_traces,
    delete_traces,
    extend_traces,
    output_plotly,
    plotly_js,
    prepend_traces,
    relayout,
    render_plotly,
    restyle,
    update,
)

CLICK_TO_INPUT = """
document.getElementById('{plot_id}').on('plotly_click', function (ev) {
    Shiny.setInputValue('clicked', ev.points[0].x, {priority: 'event'});
});
"""


def bars(n: int) -> go.Figure:
    xs = [f"x{i}" for i in range(n)]
    return go.Figure(go.Bar(x=xs, y=list(range(1, n + 1))))


def make_app() -> App:
    """Fill card plus fixed-height plot; ``plotly_js()`` loaded with the page."""
    app_ui = ui.page_fillable(
        ui.input_slider("n", "Bars", min=1, max=50, value=3),
        ui.input_checkbox("fail", "Fail"),
        ui.input_checkbox("hide", "Hide"),
        ui.output_text("click_info"),
        ui.card(
            ui.card_header("card"),
            output_plotly("fig"),
            full_screen=True,
            id="card",
        ),
        output_plotly("fixed"),
        plotly_js(),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        clicks = reactive.value(0)

        @reactive.calc
        def n() -> int:
            return int(input.n())

        @render_plotly(post_script=CLICK_TO_INPUT)
        def fig():
            if input.fail():
                raise ValueError("the figure failed on purpose")
            if input.hide():
                return None
            return bars(n())

        @render_plotly(height="220px", width="400px")
        def fixed():
            # uirevision keeps the zoom a user dragged across re-renders.
            return bars(n()).update_layout(uirevision="keep")

        @reactive.effect
        @reactive.event(input.clicked)
        def _count_clicks():
            clicks.set(clicks() + 1)

        @render.text
        def click_info():
            if clicks() == 0:
                return "clicked nothing"
            return f"clicked {input.clicked()} ({clicks()})"

    return App(app_ui, server)


def make_lazy_app() -> App:
    """No page-level ``plotly_js()``: the bundle must arrive with the output."""
    app_ui = ui.page_fluid(output_plotly("lazy"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def lazy():
            return bars(2)

    return App(app_ui, server)


# --- events to inputs -------------------------------------------------------------------


def as_text(value: Any) -> str:
    """Sorted JSON, so a test can parse exactly what the server received."""
    return json.dumps(value, sort_keys=True)


@module.ui
def events_mod_ui():
    return ui.div(output_plotly("fig", height="200px", width="300px"), ui.output_text("out"))


@module.server
def events_mod_server(input: Inputs, output: Outputs, session: Session):
    @render_plotly(events="click")
    def fig():
        return bars(3)

    @render.text
    def out():
        return as_text(input.fig_click()["points"][0]["x"]) if input.fig_click.is_set() else "-"


def make_events_app() -> App:
    """Every event wired to an input, echoed as JSON; a module instance checks namespacing."""
    app_ui = ui.page_fluid(
        ui.input_slider("n", "Bars", min=2, max=8, value=3),
        output_plotly("fig", height="300px", width="500px"),
        output_plotly("sel", height="300px", width="500px"),
        output_plotly("dense", height="300px", width="500px"),
        output_plotly("leg", height="300px", width="500px"),
        ui.output_text("click_out"),
        ui.output_text("click_count"),
        ui.output_text("hover_out"),
        ui.output_text("relayout_out"),
        ui.output_text("selected_out"),
        ui.output_text("dense_out"),
        ui.output_text("legendclick_out"),
        ui.output_text("legenddbl_out"),
        ui.output_text("dbl_out"),
        events_mod_ui("m"),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        clicks = reactive.value(0)

        @render_plotly(events=("click", "hover", "relayout"))
        def fig():
            n = int(input.n())
            # 2-D numpy customdata travels as bdata; the event must hand back plain lists.
            fig = bars(n)
            fig.data[0].customdata = np.arange(2 * n).reshape(n, 2)
            return fig

        @render_plotly(events=["selected"])
        def sel():
            return bars(4).update_layout(dragmode="select")

        @render_plotly(events="selected", max_event_points=3)
        def dense():
            fig = go.Figure(go.Scatter(x=list(range(8)), y=[1] * 8, mode="markers"))
            return fig.update_layout(dragmode="select")

        @render_plotly(events=("doubleclick", "legendclick", "legenddoubleclick"))
        def leg():
            return go.Figure(
                [go.Scatter(y=[1, 2], name="alpha"), go.Scatter(y=[2, 1], name="beta")]
            )

        @reactive.effect
        @reactive.event(input.fig_click)
        def _count():
            clicks.set(clicks() + 1)

        @render.text
        def click_out():
            return as_text(input.fig_click()) if input.fig_click.is_set() else "-"

        @render.text
        def click_count():
            return str(clicks())

        @render.text
        def hover_out():
            return as_text(input.fig_hover()) if input.fig_hover.is_set() else "-"

        @render.text
        def relayout_out():
            return as_text(input.fig_relayout()) if input.fig_relayout.is_set() else "-"

        @render.text
        def selected_out():
            return as_text(input.sel_selected()) if input.sel_selected.is_set() else "-"

        @render.text
        def dense_out():
            return as_text(input.dense_selected()) if input.dense_selected.is_set() else "-"

        @render.text
        def legendclick_out():
            return as_text(input.leg_legendclick()) if input.leg_legendclick.is_set() else "-"

        @render.text
        def legenddbl_out():
            if not input.leg_legenddoubleclick.is_set():
                return "-"
            return as_text(input.leg_legenddoubleclick())

        @render.text
        def dbl_out():
            return as_text(input.leg_doubleclick()) if input.leg_doubleclick.is_set() else "-"

        events_mod_server("m")

    return App(app_ui, server)


# --- in-place updates -------------------------------------------------------------------


@module.ui
def live_mod_ui():
    return ui.div(
        output_plotly("fig", height="200px", width="300px"),
        ui.input_action_button("recolor", "recolor"),
    )


@module.server
def live_mod_server(input: Inputs, output: Outputs, session: Session):
    @render_plotly
    def fig():
        return bars(2)

    @reactive.effect
    @reactive.event(input.recolor)
    async def _recolor():
        await restyle("fig", {"marker.color": "rgb(255, 0, 0)"})


def make_live_app() -> App:
    """Buttons that update drawn figures in place; one update is sent before its figure."""
    app_ui = ui.page_fluid(
        ui.input_action_button("tick", "tick"),
        ui.input_action_button("recolor", "recolor"),
        ui.input_action_button("retitle", "retitle"),
        ui.input_action_button("redraw", "redraw"),
        ui.input_action_button("nowhere", "nowhere"),
        ui.input_action_button("race_second", "race second"),
        ui.input_action_button("addtrace", "add trace"),
        ui.input_action_button("deltrace", "delete trace"),
        ui.input_action_button("prepend", "prepend"),
        ui.input_action_button("combo", "combo"),
        output_plotly("live", height="300px", width="500px"),
        output_plotly("late", height="200px", width="300px"),
        output_plotly("race", height="200px", width="300px"),
        live_mod_ui("m"),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def live():
            input.redraw()  # a re-render starts over from this figure
            return go.Figure([go.Scatter(y=[1, 2, 3]), go.Scatter(y=[3, 2, 1])])

        @reactive.effect
        @reactive.event(input.tick)
        async def _tick():
            await extend_traces("live", {"y": [[input.tick() + 3]]}, indices=0, max_points=4)

        @reactive.effect
        @reactive.event(input.recolor)
        async def _recolor():
            await restyle("live", {"marker.color": "rgb(255, 0, 0)"}, indices=1)

        @reactive.effect
        @reactive.event(input.retitle)
        async def _retitle():
            await relayout("live", {"title.text": f"title {input.retitle()}"})

        @reactive.effect
        @reactive.event(input.nowhere)
        async def _nowhere():
            await relayout("absent", {"title.text": "dropped"})

        @reactive.effect
        @reactive.event(input.addtrace)
        async def _addtrace():
            await add_traces("live", go.Scatter(y=[9, 8, 7]))

        @reactive.effect
        @reactive.event(input.deltrace)
        async def _deltrace():
            await delete_traces("live", 2)

        @reactive.effect
        @reactive.event(input.prepend)
        async def _prepend():
            await prepend_traces("live", {"y": [[0], [4]]}, max_points=3)

        @reactive.effect
        @reactive.event(input.combo)
        async def _combo():
            await update(
                "live",
                restyle={"marker.color": "rgb(0, 128, 0)"},
                relayout={"title.text": "combo"},
                indices=[0],
            )

        @render_plotly
        async def late():
            await asyncio.sleep(0.8)  # the updates below are sent before this figure exists
            return go.Figure(go.Scatter(y=[1]))

        @reactive.effect
        async def _before_the_first_draw():
            await extend_traces("late", {"y": [[9]]})
            await relayout("late", {"title.text": "queued"})

        @render_plotly
        async def race():
            await asyncio.sleep(0.8)
            return go.Figure(go.Scatter(y=[1]))

        @reactive.effect
        async def _race_before_the_first_draw():
            await relayout("race", {"title.text": "first"})

        @reactive.effect
        @reactive.event(input.race_second)
        async def _race_second():
            await relayout("race", {"title.text": "second"})

        live_mod_server("m")

    return App(app_ui, server)


# --- dark mode ------------------------------------------------------------------------------


def make_dark_app() -> App:
    """The README recipe next to the theme option, both driven by the color mode input."""
    app_ui = ui.page_fillable(
        ui.input_dark_mode(id="mode", mode="light"),
        ui.input_action_button("redraw_auto", "redraw auto"),
        ui.card(output_plotly("sales")),
        ui.card(output_plotly("auto")),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def sales():
            template = "plotly_dark" if input.mode() == "dark" else "plotly"
            fig = bars(3).update_layout(template=template)
            # The page's background shows through, so the graph blends into the card.
            return fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")

        @render_plotly(theme="auto")
        def auto():
            # Independent of input.mode: a mode flip cannot re-render this output, so any
            # template change a test observes was made in the browser.
            return bars(3 + input.redraw_auto() % 2)

    return App(app_ui, server)


def make_theme_app() -> App:
    """A theme pair on a page with no color mode input: the OS preference decides."""
    app_ui = ui.page_fluid(ui.card(output_plotly("styled")))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(theme=("seaborn", {"layout": {"font": {"color": "rgb(200, 100, 50)"}}}))
        def styled():
            return bars(3)

    return App(app_ui, server)
