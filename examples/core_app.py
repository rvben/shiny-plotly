"""Shiny Core example: four plotly charts rendered without shinywidgets, one of them live.

Run with:  uv run --with shiny-plotly shiny run examples/core_app.py
"""

import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from shiny_plotly import enable_compressed_plotly_js, extend_traces, output_plotly, render_plotly

app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_slider("n", "Points", min=10, max=500, value=100),
        ui.input_select("kind", "Trace", {"scatter": "Scatter", "bar": "Bar"}),
        ui.output_text("click_info"),
    ),
    ui.layout_columns(
        ui.card(
            ui.card_header("Fills the card; try full screen"),
            output_plotly("main_plot"),
            full_screen=True,
        ),
        ui.card(
            ui.card_header("FigureWidget margins preset"),
            output_plotly("compact_plot"),
            full_screen=True,
        ),
    ),
    ui.layout_columns(
        ui.card(
            ui.card_header("Fixed height, click a point"),
            output_plotly("fixed_plot"),
        ),
        ui.card(
            ui.card_header("Live: a point a second, no re-render"),
            output_plotly("live_plot"),
        ),
    ),
    title="shiny-plotly",
    fillable=True,
)


def server(input: Inputs, output: Outputs, session: Session):
    @reactive.calc
    def data():
        rng = random.Random(input.n())
        x = list(range(input.n()))
        return x, list(accumulate(rng.gauss(0, 1) for _ in x))

    @render_plotly
    def main_plot():
        x, y = data()
        if input.kind() == "scatter":
            trace = go.Scatter(x=x, y=y, mode="lines")
        else:
            trace = go.Bar(x=x, y=y)
        # uirevision keeps the zoom the user dragged across re-renders of the same kind.
        return go.Figure(trace).update_layout(title=f"{input.n()} points", uirevision=input.kind())

    @render_plotly(figurewidget_margins=True)
    def compact_plot():
        x, y = data()
        return go.Figure(go.Scatter(x=x, y=[abs(v) for v in y], fill="tozeroy"))

    # Clicks arrive as input.fixed_plot_click (the output id plus the event name).
    @render_plotly(height="260px", events="click", config={"displaylogo": False})
    def fixed_plot():
        x, y = data()
        return go.Figure(go.Scatter(x=x, y=y, mode="markers"))

    # The render function draws the seed once; every second an effect appends one point
    # to the graph in the browser, keeping the last 60, and nothing is re-rendered.
    @render_plotly(height="260px")
    def live_plot():
        return go.Figure(go.Scatter(x=[], y=[], mode="lines")).update_layout(
            xaxis_title="tick", yaxis_title="value"
        )

    tick = 0

    @reactive.effect
    async def _stream():
        nonlocal tick
        reactive.invalidate_later(1)
        tick += 1
        point = {"x": [[tick]], "y": [[random.gauss(0, 1)]]}
        await extend_traces("live_plot", point, max_points=60)

    @render.text
    def click_info():
        if not input.fixed_plot_click.is_set():
            return "Click a point in the bottom chart."
        pt = input.fixed_plot_click()["points"][0]
        return f"Clicked x={pt['x']}, y={pt['y']:.2f}"


app = App(app_ui, server)
# Optional: serve plotly.js compressed from the very first request instead of from the
# first session on (see README, "plotly.js on the wire").
enable_compressed_plotly_js(app)
