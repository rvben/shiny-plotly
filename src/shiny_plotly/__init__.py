"""Render plotly figures in Shiny for Python without the shinywidgets layer."""

from ._deps import __version__, plotly_js, shiny_plotly_js
from ._html import FIGUREWIDGET_MARGINS, fig_to_ui
from ._render import output_plotly, render_plotly

__all__ = (
    "FIGUREWIDGET_MARGINS",
    "__version__",
    "fig_to_ui",
    "output_plotly",
    "plotly_js",
    "render_plotly",
    "shiny_plotly_js",
)
