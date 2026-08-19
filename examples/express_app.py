"""Shiny Express example: the decorator creates its own output placeholder.

Run with:  uv run --with shiny-plotly shiny run examples/express_app.py
"""

import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny.express import input, ui

from shiny_plotly import plotly_js, render_plotly

ui.page_opts(title="shiny-plotly (Express)", fillable=True)
plotly_js()

with ui.sidebar():
    ui.input_slider("n", "Points", min=10, max=500, value=100)

with ui.card(full_screen=True):
    ui.card_header("Random walk")

    @render_plotly
    def walk():
        rng = random.Random(input.n())
        y = list(accumulate(rng.gauss(0, 1) for _ in range(input.n())))
        return go.Figure(go.Scatter(y=y, mode="lines"))
