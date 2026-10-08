from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any, cast

import plotly.io as pio
from _plotly_utils import utils as plotly_utils
from htmltools import HTML, Tag, TagList, css, tags
from plotly.basedatatypes import BaseFigure

from ._bundle import check_figure
from ._deps import plotly_js, shiny_plotly_js
from ._serve import enable_for_current_session
from ._snapshot import snapshot

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
    check_figure(fig_dict)
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


def as_fig_dict(fig: Figure, *, preserve_arrays: bool = False) -> dict[str, Any]:
    """
    The figure as a plain dict carrying a ``layout`` dict, whichever way it was given.

    Everything downstream reaches into ``layout`` (to fill in margins, to drop a baked-in
    template), so it is made a dict here rather than guarded against at each of them.
    """
    # Figure.to_dict() does no validation (the figure was validated when built), and always
    # carries a layout; a dict is passed through as the caller's JSON, so pio.to_html gets
    # validate=False and never reconstructs a Figure from it.
    if isinstance(fig, BaseFigure):
        if preserve_arrays:
            if type(fig).to_dict is not BaseFigure.to_dict:
                return deepcopy(fig.to_dict())  # respect custom Figure serializers
            # Figure.to_dict() encodes full numpy arrays as base64 in plotly 6. A
            # sampler would immediately decode them again. The public component
            # serializers copy the same properties while retaining native arrays,
            # so sampling can run before encoding anything sent to the browser.
            result = {
                "data": [snapshot(trace) for trace in cast(Any, fig).data],
                "layout": snapshot(fig.layout),
            }
            if fig.frames:
                result["frames"] = [snapshot(frame) for frame in fig.frames]
            return result
        return fig.to_dict()
    if isinstance(fig, dict):
        return {**fig, "layout": dict(fig.get("layout") or {})}
    raise TypeError(
        f"shiny-plotly expects a plotly go.Figure (or its dict), got {type(fig).__name__}"
    )


def encode_figure_arrays(fig_dict: dict[str, Any], *, sampled_traces: Iterable[str] = ()) -> None:
    """Apply Plotly's figure array encoding after sampling, when the version supports it."""
    # Plotly exposes no public raw-array Figure serializer or standalone typed-array
    # encoder. Reuse exactly the conversion its Figure.to_dict() calls, rather than
    # reconstructing and revalidating a Figure or duplicating dtype/shape rules. Plotly
    # 5 has no binary conversion; its JSON encoder already handles native arrays.
    convert = getattr(plotly_utils, "convert_to_base64", None)
    if convert is not None:
        # Keep sampled trace encoding unchanged. Whole traces, layout arrays and
        # frames retain Plotly's compact binary representation. The wrapper shares
        # their copied properties.
        sampled = set(sampled_traces)
        convert(
            {
                **fig_dict,
                "data": [
                    trace for i, trace in enumerate(fig_dict["data"]) if str(i) not in sampled
                ],
            }
        )


def fill_in_margins(fig_dict: dict[str, Any]) -> None:
    layout = fig_dict["layout"]
    layout["margin"] = {**FIGUREWIDGET_MARGINS, **(layout.get("margin") or {})}
