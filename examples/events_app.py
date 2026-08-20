"""Events example: clicks and box selections arrive as Shiny inputs.

The trace holds 50 000 points, above the default max_event_points of 10 000, so a big
selection arrives as a count plus the box's geometry and the server recounts membership
from its own data, the pattern the README describes for dense traces.

Run with:  uv run --with shiny-plotly shiny run examples/events_app.py
"""

import random

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, render, ui

from shiny_plotly import output_plotly, render_plotly

rng = random.Random(7)
N = 50_000
X = [rng.gauss(0, 1) for _ in range(N)]
Y = [rng.gauss(0, 1) for _ in range(N)]

app_ui = ui.page_fillable(
    ui.card(
        ui.card_header("Drag a box; click a point"),
        output_plotly("cloud"),
        full_screen=True,
    ),
    ui.output_text("selection_info"),
    ui.output_text("click_info"),
    title="shiny-plotly events",
)


def server(input: Inputs, output: Outputs, session: Session):
    @render_plotly(events=("click", "selected"))
    def cloud():
        fig = go.Figure(go.Scattergl(x=X, y=Y, mode="markers", marker={"size": 3}))
        return fig.update_layout(dragmode="select")

    @render.text
    def selection_info():
        if not input.cloud_selected.is_set():
            return "Selection: none yet."
        sel = input.cloud_selected()
        if sel is None:
            return "Selection: cleared."
        if sel["points"] is not None:
            return f"Selection: {len(sel['points'])} points arrived with the event."
        # Above max_event_points the geometry arrives instead of the points; the data
        # lives here, so membership is a filter on the box the user dragged.
        x0, x1 = sorted(sel["range"]["x"])
        y0, y1 = sorted(sel["range"]["y"])
        inside = sum(1 for x, y in zip(X, Y, strict=True) if x0 <= x <= x1 and y0 <= y <= y1)
        return f"Selection: {sel['point_count']} points; recounted {inside} on the server."

    @render.text
    def click_info():
        if not input.cloud_click.is_set():
            return "Click: none yet."
        pt = input.cloud_click()["points"][0]
        return f"Click: point {pt['pointNumber']} at x={pt['x']:.2f}, y={pt['y']:.2f}."


app = App(app_ui, server)
