"""The live demo behind https://rvben.github.io/shiny-plotly/, running in the browser.

The whole app runs in the browser via pyodide: no server. Two tabs show the package's
two sides. Stream draws one empty figure and feeds it through ``extend_traces``, one
small message per tick, no re-render. Explore forwards plotly's click and box-select
events to Shiny inputs. Both figures follow the page's color mode via ``theme="auto"``
and the toggle in the corner.

Export this directory to a static site and serve it:

    uvx shinylive export examples/shinylive site
    python -m http.server 8000 -d site

That installs the released package from PyPI (requirements.txt); the deployed demo is
built by ``make site``, which installs the wheel from the current checkout instead.
"""

import random
from datetime import datetime

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from shiny_plotly import extend_traces, output_plotly, render_plotly

TICK_SECONDS = 0.5
CLUSTERS = {"alpha": (0.0, 0.0), "beta": (3.0, 2.0), "gamma": (1.0, 4.0)}

app_ui = ui.page_fillable(
    ui.div(
        ui.tags.b("shiny-plotly"),
        ui.span("plotly figures in Shiny for Python, without shinywidgets", class_="text-muted"),
        ui.div(ui.input_dark_mode(), class_="ms-auto"),
        class_="d-flex align-items-center gap-3",
    ),
    ui.navset_card_underline(
        ui.nav_panel(
            "Stream",
            ui.layout_columns(
                ui.input_switch("run", "Stream", value=True),
                ui.input_slider("window", "Window (points)", min=50, max=500, value=200, step=50),
                col_widths=(3, 9),
            ),
            output_plotly("stream"),
        ),
        ui.nav_panel(
            "Explore",
            output_plotly("explore"),
            ui.layout_columns(ui.output_text("clicked"), ui.output_text("picked")),
        ),
    ),
    title="shiny-plotly demo",
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
        # One column of new values per trace; max_points keeps the rolling window.
        await extend_traces(
            "stream",
            {"x": [[now], [now]], "y": [[state["a"]], [state["b"]]]},
            max_points=input.window(),
        )

    @render_plotly(theme="auto", events=("click", "selected"))
    def explore():
        rng = random.Random(7)
        fig = go.Figure()
        for name, (cx, cy) in CLUSTERS.items():
            fig.add_scatter(
                x=[cx + rng.gauss(0, 0.8) for _ in range(40)],
                y=[cy + rng.gauss(0, 0.8) for _ in range(40)],
                mode="markers",
                name=name,
            )
        # Select mode from the start, so a plain drag is a box selection.
        return fig.update_layout(dragmode="select")

    @render.text
    def clicked():
        if not input.explore_click.is_set():
            return "Click a point."
        point = input.explore_click()["points"][0]
        name = list(CLUSTERS)[point["curveNumber"]]
        return f"Clicked {name} at ({point['x']:.2f}, {point['y']:.2f})."

    @render.text
    def picked():
        if not input.explore_selected.is_set() or input.explore_selected() is None:
            return "Drag a box to select points."
        return f"{len(input.explore_selected()['points'])} points selected."


app = App(app_ui, server)
