"""Shiny apps exercised by the browser tests."""

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from shiny_plotly import output_plotly, plotly_js, render_plotly

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
