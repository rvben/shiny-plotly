from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import plotly.io as pio
from htmltools import Tag, css, tags
from shiny.render.renderer import Jsonifiable, Renderer, ValueFn

from ._deps import plotly_js, shiny_plotly_js
from ._html import DEFAULT_CONFIG, Figure, as_fig_dict, fill_in_margins
from ._serve import enable_compressed_plotly_js

__all__ = ("output_plotly", "render_plotly")


def output_plotly(id: str, *, width: str | None = None, height: str | None = None) -> Tag:
    """
    Placeholder for a :func:`render_plotly` output. A drop-in for ``output_widget(id)``.

    The browser helper binds it and draws each figure into one graph div that it keeps
    across re-renders (``Plotly.react``), so zoom and pan survive a re-render when the
    figure sets ``layout.uirevision``, and event handlers attached by ``post_script`` stay
    attached. The tag carries the plotly.js bundle and the helper, so nothing else needs to
    be added to the page.

    It is fill-aware: inside ``ui.card(full_screen=True)`` or a fillable page the plot
    grows and shrinks with its container. Passing ``height`` fixes the output's height
    instead (the plot fills that height), the same rule ``output_widget`` follows.
    """
    fill = height is None
    return tags.div(
        plotly_js(),
        shiny_plotly_js(),
        id=id,
        class_="shiny-plotly-output html-fill-container" + (" html-fill-item" if fill else ""),
        style=css(width=width, height=height),
    )


class render_plotly(Renderer[Figure]):
    """
    Render a plotly figure into an :func:`output_plotly` output.

    A drop-in for ``@render_widget`` when the function returns a ``go.Figure`` (or its
    dict). Use bare (``@render_plotly``) or with options::

        @render_plotly(height="300px", figurewidget_margins=True)
        def sales():
            return px.bar(df, x="month", y="total")

    The figure travels as plotly JSON and the browser helper draws it with
    ``Plotly.newPlot`` the first time and ``Plotly.react`` after that, into the same graph
    div. Returning ``None`` empties the output. Works in Core and Express; the decorated
    function may be sync or async.

    Parameters
    ----------
    height, width
        CSS size of the plot. ``height=None`` (default) fills the output; a value such as
        ``"300px"`` fixes it. See :func:`~shiny_plotly.fig_to_ui`.
    figurewidget_margins
        Fill in unset margin sides with the values shinywidgets applies to a FigureWidget
        (l16/t32/r16/b16), so a migrated app keeps its exact look.
    config
        Extra plotly config, merged over ``{"responsive": True}``.
    post_script
        JavaScript run once the graph div exists and the first figure is drawn, with
        ``{plot_id}`` replaced by the graph div's id. The place to forward plotly events to
        Shiny inputs; the handlers stay attached across re-renders.
    """

    def __init__(
        self,
        _fn: ValueFn[Figure | None] | None = None,
        *,
        height: str | None = None,
        width: str = "100%",
        figurewidget_margins: bool = False,
        config: Mapping[str, Any] | None = None,
        post_script: str | None = None,
    ) -> None:
        self.height = height
        self.width = width
        self.figurewidget_margins = figurewidget_margins
        self.config = config
        self.post_script = post_script
        # Registers _fn (sets output_id from its name) when used as a bare decorator.
        super().__init__(_fn)  # type: ignore[arg-type]

    def __call__(self, _fn: ValueFn[Figure | None]) -> render_plotly:  # type: ignore[override]
        super().__call__(_fn)
        # Registration happens inside the session, the first point at which the app is
        # reachable; from here on plotly.min.js is served compressed and immutable.
        if self._session is not None:
            enable_compressed_plotly_js(getattr(self._session, "app", None))
        return self

    def auto_output_ui(self) -> Tag:
        return output_plotly(self.output_id)

    async def transform(self, value: Figure) -> Jsonifiable:
        fig_dict = as_fig_dict(value)
        if self.figurewidget_margins:
            fill_in_margins(fig_dict)
        return {
            # Serialised by plotly, not Shiny: numpy and pandas values, datetimes and the
            # compact base64 array encoding only work through plotly's encoder.
            "figure": pio.to_json(fig_dict, validate=False, remove_uids=False),
            "config": {**DEFAULT_CONFIG, **(self.config or {})},
            "height": self.height,
            "width": self.width,
            "post_script": self.post_script,
        }
