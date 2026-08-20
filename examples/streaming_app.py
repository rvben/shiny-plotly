"""Streaming example: a live rolling window fed by extend_traces, no re-render.

The figure is drawn once, empty; every point after that arrives through
``extend_traces``, which appends in the browser and drops the oldest points past the
window. Nothing re-renders, so the stream costs one small message per tick and a zoom
or pan the user dragged stays where they put it. The switch pauses the stream; the
slider resizes the window from the next tick on.

Run with:  uv run --with shiny-plotly shiny run examples/streaming_app.py
"""

import random
from datetime import datetime

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, ui

from shiny_plotly import extend_traces, output_plotly, render_plotly

TICK_SECONDS = 0.25

app_ui = ui.page_fillable(
    ui.layout_columns(
        ui.input_switch("run", "Stream", value=True),
        ui.input_slider("window", "Window (points)", min=50, max=1000, value=300, step=50),
        col_widths=(3, 9),
    ),
    ui.card(
        ui.card_header("Two sensors, one point per tick"),
        output_plotly("stream"),
        full_screen=True,
    ),
    title="shiny-plotly streaming",
)


def server(input: Inputs, output: Outputs, session: Session):
    # Per-session: each visitor gets their own stream.
    state = {"a": 20.0, "b": 50.0}

    @render_plotly(theme="auto")
    def stream():
        fig = go.Figure(
            [
                go.Scatter(x=[], y=[], mode="lines", name="sensor a"),
                go.Scatter(x=[], y=[], mode="lines", name="sensor b"),
            ]
        )
        return fig.update_layout(uirevision="keep")

    @reactive.effect
    async def _tick():
        reactive.invalidate_later(TICK_SECONDS)
        if not input.run():
            return
        now = datetime.now()
        state["a"] += random.uniform(-1.0, 1.0)
        state["b"] += random.uniform(-2.0, 2.0)
        # One column of new values per trace; max_points keeps the rolling window. Sent
        # while the first (empty) figure is still on its way, the update is held in the
        # browser and applied right after the draw, so nothing is lost at startup.
        await extend_traces(
            "stream",
            {"x": [[now], [now]], "y": [[state["a"]], [state["b"]]]},
            max_points=input.window(),
        )


app = App(app_ui, server)
