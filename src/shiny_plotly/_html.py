from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import plotly.io as pio
from htmltools import HTML, Tag, TagList, css, tags
from plotly.basedatatypes import BaseFigure

from ._deps import plotly_js, shiny_plotly_js
from ._serve import enable_for_current_session

__all__ = ("FIGUREWIDGET_MARGINS", "fig_to_ui")

# The margins shinywidgets installs on every plotly FigureWidget it renders (its
# set_layout_defaults: template.layout.margin = l16/t32/r16/b16), so a migrated app can
# keep its exact look. Plotly's own defaults are l80/t100/r80/b80.
FIGUREWIDGET_MARGINS: Mapping[str, int] = MappingProxyType({"l": 16, "t": 32, "r": 16, "b": 16})

DEFAULT_CONFIG: Mapping[str, Any] = MappingProxyType({"responsive": True})

# Height of a filling container when nothing constrains it, and its flex basis inside a
# fill layout; the same 400px shinywidgets gives a FigureWidget.
_FILL_BASIS = "400px"

# Runs right after Plotly.newPlot resolves: hands the graph div to the browser helper
# (shiny-plotly.js), which tracks its size and purges it once it leaves the document.
_TRACK_SCRIPT = "window.shinyPlotly && shinyPlotly.track(document.getElementById('{plot_id}'));"

Figure = BaseFigure | dict[str, Any]
"""A ``plotly.graph_objects.Figure`` or its JSON dict (``fig.to_dict()``)."""


def fig_to_ui(
    fig: Figure | None,
    div_id: str | None = None,
    *,
    height: str | None = None,
    width: str = "100%",
    figurewidget_margins: bool = False,
    config: Mapping[str, Any] | None = None,
    post_script: str | None = None,
) -> TagList | None:
    """
    Turn a plotly figure into a Shiny UI fragment that draws it with ``Plotly.newPlot``.

    This is the lower-level path for a ``@render.ui`` that composes a figure with other
    UI, or for any htmltools context; each render draws a fresh graph. An output that is
    only a figure is better served by :class:`~shiny_plotly.render_plotly`, which keeps
    the graph across re-renders.

    Parameters
    ----------
    fig
        A ``go.Figure`` or its JSON dict. ``None`` renders nothing (returns ``None``).
    div_id
        DOM id of the plotly graph div. A fresh id is generated when omitted.
    height
        CSS height of the plot. ``None`` (the default) fills the parent: inside a fill
        layout (``ui.card(full_screen=True)``, a fillable page) the plot grows and shrinks
        with it from a 400px basis; anywhere else it is 400px tall. A value such as
        ``"300px"`` fixes the height and opts out of filling, exactly like
        ``output_widget(height=...)`` does in shinywidgets.
    width
        CSS width of the plot, ``"100%"`` by default.
    figurewidget_margins
        Fill in margin sides the figure left unset with :data:`FIGUREWIDGET_MARGINS`, the
        values shinywidgets applies to a FigureWidget. Sides the figure sets explicitly win.
        The caller's figure object is never mutated.
    config
        Extra ``Plotly.newPlot`` config, merged over ``{"responsive": True}``.
    post_script
        JavaScript run after the plot is drawn; ``{plot_id}`` is replaced with the graph div
        id. The place to bind plotly events back to Shiny inputs.
    """
    if fig is None:
        return None
    enable_for_current_session()
    fig_dict = as_fig_dict(fig)
    if figurewidget_margins:
        fill_in_margins(fig_dict)
    if div_id is None:
        div_id = "plotly-" + uuid.uuid4().hex

    fragment = pio.to_html(
        fig_dict,
        validate=False,
        full_html=False,
        include_plotlyjs=False,
        include_mathjax=False,
        div_id=div_id,
        config={**DEFAULT_CONFIG, **(config or {})},
        post_script=[_TRACK_SCRIPT, post_script] if post_script else [_TRACK_SCRIPT],
    )
    container: Tag = tags.div(
        HTML(fragment),
        class_="shiny-plotly html-fill-item" if height is None else "shiny-plotly",
        style=css(height=height or _FILL_BASIS, width=width),
    )
    return TagList(plotly_js(), shiny_plotly_js(), container)


def as_fig_dict(fig: Figure) -> dict[str, Any]:
    # Figure.to_dict() does no validation (the figure was validated when built), and a
    # dict is passed through as the caller's JSON; pio.to_html gets validate=False so it
    # never reconstructs a Figure from it.
    if isinstance(fig, BaseFigure):
        return fig.to_dict()
    if isinstance(fig, dict):
        return {**fig, "layout": dict(fig.get("layout") or {})}
    raise TypeError(
        f"shiny-plotly expects a plotly go.Figure (or its dict), got {type(fig).__name__}"
    )


def fill_in_margins(fig_dict: dict[str, Any]) -> None:
    layout = fig_dict.setdefault("layout", {})
    layout["margin"] = {**FIGUREWIDGET_MARGINS, **(layout.get("margin") or {})}
