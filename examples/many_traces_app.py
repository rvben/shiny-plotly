"""Change one series and its title with one small update, keeping other traces.

Run: uv run --with numpy shiny run examples/many_traces_app.py
"""

import math

import numpy as np
import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, ui

from shiny_plotly import output_plotly, render_plotly, update

TRACE_COUNT = 100
POINT_COUNT = 1250
X = np.arange(POINT_COUNT)
Y = np.sin(X / 40)

app_ui = ui.page_fillable(
    ui.layout_columns(
        ui.input_numeric("series", "Series", value=0, min=0, max=TRACE_COUNT - 1, step=1),
        ui.input_slider("offset", "Offset", min=-2, max=2, value=0, step=0.1),
        ui.input_action_button("apply", "Apply"),
        col_widths=(3, 6, 3),
    ),
    ui.card(output_plotly("chart"), full_screen=True),
    title="Update one series",
)


def server(input: Inputs, output: Outputs, session: Session):
    @render_plotly
    def chart():
        return go.Figure(
            [
                go.Scattergl(x=X, y=Y + i / 10, name=f"Series {i}", mode="lines")
                for i in range(TRACE_COUNT)
            ]
        ).update_layout(uirevision="keep", showlegend=False)

    @reactive.effect
    @reactive.event(input.apply)
    async def change():
        selected = input.series()
        if (
            selected is None
            or not math.isfinite(selected)
            or selected != math.floor(selected)
            or not 0 <= selected < TRACE_COUNT
        ):
            ui.notification_show(
                f"Choose a whole-number series from 0 to {TRACE_COUNT - 1}.", type="warning"
            )
            return
        index = int(selected)
        # The render function reads no controls: it draws once. Explicitly update
        # one trace and the title in one redraw. Plotly preserves the user's zoom.
        await update(
            "chart",
            restyle={"y": [Y + index / 10 + input.offset()]},
            relayout={"title.text": f"Changed series {index}"},
            indices=index,
        )


app = App(app_ui, server)
