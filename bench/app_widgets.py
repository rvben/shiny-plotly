"""The benchmark app rendered through shinywidgets (its ``render_plotly``, which wraps the
figure in a FigureWidget)."""

from __future__ import annotations

from shiny import App, Inputs, Outputs, Session
from shinywidgets import output_widget, render_plotly

from bench.common import figure, page


def server(input: Inputs, output: Outputs, session: Session):
    @render_plotly
    def fig():
        return figure(int(input.n()))


app = App(page(output_widget("fig")), server)
