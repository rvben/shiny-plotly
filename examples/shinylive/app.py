"""Shinylive example: the whole app runs in the browser via pyodide, no server.

Export this directory to a static site and serve it:

    uvx shinylive export examples/shinylive site
    python -m http.server 8000 -d site
"""

import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, ui

from shiny_plotly import output_plotly, render_plotly

app_ui = ui.page_fillable(
    ui.input_slider("n", "Points", min=10, max=500, value=100),
    ui.card(output_plotly("walk"), full_screen=True),
    title="shiny-plotly on shinylive",
)


def server(input: Inputs, output: Outputs, session: Session):
    # theme="auto" follows the embedding page's color mode, OS preference included.
    @render_plotly(theme="auto")
    def walk():
        rng = random.Random(input.n())
        y = list(accumulate(rng.gauss(0, 1) for _ in range(input.n())))
        return go.Figure(go.Scatter(y=y, mode="lines"))


app = App(app_ui, server)
