"""The benchmark app rendered through shiny-plotly."""

from __future__ import annotations

from shiny import App, Inputs, Outputs, Session

from bench.common import figure, page
from shiny_plotly import output_plotly, render_plotly


def server(input: Inputs, output: Outputs, session: Session):
    @render_plotly
    def fig():
        return figure(int(input.n()))


app = App(page(output_plotly("fig")), server)
