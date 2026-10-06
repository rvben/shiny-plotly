"""Resampling example: a million points a second apart, drawn as a sample of 2000.

The page gets a few thousand points instead of a million; the full series stays in the
session on the server. Zoom in and the visible range is redrawn from the full data, so
the detail is there at any zoom: the one-second glitch at 12:00 on day 5 shows as a spike
in the overview and as a single point when zoomed in, and the sensor outage on day 8 is a
gap at every zoom. A click reports the clicked point's position in the full series.

Run with:  uv run --with 'shiny-plotly[resample]' shiny run examples/resample_app.py
"""

import numpy as np
import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, render, ui

from shiny_plotly import output_plotly, render_plotly

N = 1_000_000
rng = np.random.default_rng(7)
T = np.datetime64("2026-01-01T00:00:00") + np.arange(N).astype("timedelta64[s]")
Y = 20 + np.cumsum(rng.normal(0, 0.02, N)) + 3 * np.sin(np.arange(N) / 86_400 * 2 * np.pi)
GLITCH = 4 * 86_400 + 12 * 3_600
Y[GLITCH] += 25
OUTAGE = slice(7 * 86_400, 7 * 86_400 + 5 * 3_600)
Y[OUTAGE] = np.nan

app_ui = ui.page_fillable(
    ui.card(
        ui.card_header(f"{N:,} readings, resampled on zoom"),
        output_plotly("readings"),
        full_screen=True,
    ),
    ui.output_text("click_info"),
    title="shiny-plotly resampling",
)


def server(input: Inputs, output: Outputs, session: Session):
    @render_plotly(resample=2000, events="click")
    def readings():
        fig = go.Figure(go.Scattergl(x=T, y=Y, mode="lines", name="sensor"))
        return fig.update_layout(uirevision="readings", margin={"t": 16})

    @render.text
    def click_info():
        if not input.readings_click.is_set():
            return "Click a point."
        point = input.readings_click()["points"][0]
        n = point["pointNumber"]
        return f"Point {n:,} of {N:,}: {T[n]}, {Y[n]:.2f} (clicked {point['y']:.2f})"


app = App(app_ui, server)
