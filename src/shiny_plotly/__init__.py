"""Render plotly figures in Shiny for Python without the shinywidgets layer."""

from ._deps import __version__, plotly_js, shiny_plotly_js
from ._html import FIGUREWIDGET_MARGINS, fig_to_ui
from ._render import DEFAULT_MAX_EVENT_POINTS, output_plotly, render_plotly
from ._serve import enable_compressed_plotly_js
from ._update import extend_traces, relayout, restyle

__all__ = (
    "DEFAULT_MAX_EVENT_POINTS",
    "FIGUREWIDGET_MARGINS",
    "__version__",
    "enable_compressed_plotly_js",
    "extend_traces",
    "fig_to_ui",
    "output_plotly",
    "plotly_js",
    "relayout",
    "render_plotly",
    "restyle",
    "shiny_plotly_js",
)
