"""Shared by the two benchmark apps: the same page and the same figure.

Each app is a fillable page with a slider and one card holding the figure. The figure is a
line trace of ``n`` points; the slider re-renders it. The card has full_screen enabled in
both so the sizing machinery each package installs is part of what is measured. The apps
live in separate modules so each server process imports only its own stack.
"""

from __future__ import annotations

import plotly.graph_objects as go
from htmltools import Tag
from shiny import ui


def figure(n: int) -> go.Figure:
    xs = list(range(n))
    return go.Figure(go.Scatter(x=xs, y=[(i * 7919) % 101 for i in xs], mode="lines"))


def page(output: Tag) -> Tag:
    return ui.page_fillable(
        ui.input_slider("n", "Points", min=10, max=2000, value=100),
        ui.card(ui.card_header("figure"), output, full_screen=True),
    )
