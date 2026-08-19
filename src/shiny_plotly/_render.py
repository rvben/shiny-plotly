from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from htmltools import Tag, css
from shiny import ui as _ui
from shiny.render import ui as _render_ui
from shiny.render.renderer import Jsonifiable, ValueFn
from shiny.session import require_active_session

from ._html import Figure, fig_to_ui

__all__ = ("output_plotly", "render_plotly")


def output_plotly(id: str, *, width: str | None = None, height: str | None = None) -> Tag:
    """
    Placeholder for a :func:`render_plotly` output. A drop-in for ``output_widget(id)``.

    It is ``ui.output_ui`` made fill-aware: inside ``ui.card(full_screen=True)`` or a
    fillable page the plot grows and shrinks with its container. Passing ``height`` fixes
    the output's height instead (the plot fills that height), the same rule
    ``output_widget`` follows. ``ui.output_ui(id)`` also works when no fill behaviour is
    wanted.
    """
    return _ui.output_ui(
        id, fill=height is None, fillable=True, style=css(width=width, height=height)
    )


class render_plotly(_render_ui):
    """
    Render a plotly figure as plain HTML drawn with ``Plotly.newPlot``.

    A drop-in for ``@render_widget`` when the function returns a ``go.Figure`` (or its
    dict). Use bare (``@render_plotly``) or with options::

        @render_plotly(height="300px", figurewidget_margins=True)
        def sales():
            return px.bar(df, x="month", y="total")

    Returning ``None`` renders nothing. Works in Core and Express; the decorated function
    may be sync or async.

    Parameters
    ----------
    height, width
        CSS size of the plot. ``height=None`` (default) fills the output; a value such as
        ``"300px"`` fixes it. See :func:`~shiny_plotly.fig_to_ui`.
    figurewidget_margins
        Fill in unset margin sides with the values shinywidgets applies to a FigureWidget
        (l16/t32/r16/b16), so a migrated app keeps its exact look.
    config
        Extra ``Plotly.newPlot`` config, merged over ``{"responsive": True}``.
    post_script
        JavaScript run after the plot is drawn, with ``{plot_id}`` replaced by the graph
        div's id. The place to forward plotly events to Shiny inputs.
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

    def auto_output_ui(self) -> Tag:
        return output_plotly(self.output_id)

    async def transform(self, value: Figure) -> Jsonifiable:  # type: ignore[override]
        session = require_active_session(None)
        fragment = fig_to_ui(
            value,
            div_id=f"{session.ns(self.output_id)}-plotly",
            height=self.height,
            width=self.width,
            figurewidget_margins=self.figurewidget_margins,
            config=self.config,
            post_script=self.post_script,
        )
        return await super().transform(fragment)
