"""Dark mode example: theme="auto" follows the page's color mode in the browser.

Run with:  uv run --with shiny-plotly shiny run examples/dark_app.py
"""

import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, ui

from shiny_plotly import output_plotly, render_plotly

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
TOTALS = [12, 17, 9, 20, 14, 23]

app_ui = ui.page_fillable(
    ui.input_dark_mode(),
    ui.layout_columns(
        ui.card(
            ui.card_header("theme='auto': plotly in light, plotly_dark in dark"),
            output_plotly("auto_themed"),
            full_screen=True,
        ),
        ui.card(
            ui.card_header("A custom (light, dark) pair"),
            output_plotly("pair_themed"),
            full_screen=True,
        ),
    ),
    title="shiny-plotly dark mode",
)

# Any registered template name, plotly Template object or template dict works in the pair.
DARK_TEMPLATE = {
    "layout": {
        "font": {"color": "#f2f5fa"},
        "colorway": ["#66c2a5", "#fc8d62", "#8da0cb"],
    }
}


def server(input: Inputs, output: Outputs, session: Session):
    # The flip happens in the browser with Plotly.relayout: no server round-trip, and it
    # works even while the server is busy. Both templates travel with the figure, their
    # backgrounds made transparent so the card shows through in both modes.
    @render_plotly(theme="auto")
    def auto_themed():
        return go.Figure(go.Bar(x=MONTHS, y=TOTALS))

    @render_plotly(theme=("seaborn", DARK_TEMPLATE))
    def pair_themed():
        return go.Figure(go.Scatter(x=MONTHS, y=TOTALS, mode="lines+markers"))


app = App(app_ui, server)
