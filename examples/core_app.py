"""Shiny Core example: three plotly charts rendered without shinywidgets.

Run with:  uv run --with shiny-plotly shiny run examples/core_app.py
"""

import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from shiny_plotly import output_plotly, render_plotly

# Forwards plotly click events to a Shiny input. {plot_id} is the graph div's id.
CLICK_TO_INPUT = """
document.getElementById('{plot_id}').on('plotly_click', function (ev) {
    var p = ev.points[0];
    Shiny.setInputValue('clicked', {x: p.x, y: p.y}, {priority: 'event'});
});
"""

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
    ui.card(
        ui.card_header("Fixed height, click a point"),
        output_plotly("fixed_plot"),
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

    @render_plotly(height="260px", post_script=CLICK_TO_INPUT, config={"displaylogo": False})
    def fixed_plot():
        x, y = data()
        return go.Figure(go.Scatter(x=x, y=y, mode="markers"))

    @render.text
    def click_info():
        if not input.clicked.is_set():
            return "Click a point in the bottom chart."
        pt = input.clicked()
        return f"Clicked x={pt['x']}, y={pt['y']:.2f}"


app = App(app_ui, server)
