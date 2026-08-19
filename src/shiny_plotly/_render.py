from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import plotly.io as pio
from htmltools import Tag, css, tags
from shiny.module import resolve_id
from shiny.render.renderer import Jsonifiable, Renderer, ValueFn
from shiny.ui.fill import as_fill_item, as_fillable_container

from ._deps import plotly_js, shiny_plotly_js
from ._html import DEFAULT_CONFIG, Figure, as_fig_dict, fill_in_margins
from ._serve import enable_compressed_plotly_js

__all__ = ("DEFAULT_MAX_EVENT_POINTS", "EVENTS", "output_plotly", "render_plotly")

# Plotly events that can be forwarded to Shiny inputs, in the order they are sent.
EVENTS = ("click", "hover", "selected", "relayout")

# Points per event above which the browser sends the count and the selection's geometry
# instead of the points. Each point is about 100 bytes of JSON; at this cap an event is
# about 1 MB and reaches the server in well under a second, while a selection of every
# point of a 200k-point trace would be 20 MB, above the 16 MB websocket message limit
# of uvicorn's default settings, which closes the connection and the session with it.
DEFAULT_MAX_EVENT_POINTS = 10_000


def normalize_events(events: str | Iterable[str] | None) -> tuple[str, ...]:
    """The requested event names, validated, without duplicates, in :data:`EVENTS` order."""
    if events is None:
        return ()
    names = (events,) if isinstance(events, str) else tuple(events)
    unknown = [name for name in names if name not in EVENTS]
    if unknown:
        raise ValueError(
            f"unknown plotly event(s) {', '.join(map(repr, unknown))}; "
            f"events must be among {', '.join(EVENTS)}"
        )
    return tuple(name for name in EVENTS if name in names)


def normalize_max_event_points(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"max_event_points must be a positive int or None, got {value!r}")
    return value


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
    tag = tags.div(
        plotly_js(),
        shiny_plotly_js(),
        id=resolve_id(id),  # namespaced inside a module, like every Shiny output
        class_="shiny-plotly-output",
        style=css(width=width, height=height),
    )
    # The graph inside fills the output, so the output is a fillable container on every
    # page; these helpers bring the fill CSS, which a plain page does not carry by itself.
    tag = as_fillable_container(tag)
    return as_fill_item(tag) if height is None else tag


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

    Plotly events reach the server as inputs named after the output::

        @render_plotly(events=("click", "selected"))
        def sales(): ...

        @render.text
        def picked():
            event = input.sales_click()  # {"points": [{"curveNumber": 0, "x": ..., ...}]}
            ...

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
        ``{plot_id}`` replaced by the graph div's id. For anything the ``events`` option
        does not cover; handlers attached here stay attached across re-renders.
    events
        Plotly events to forward to Shiny inputs: any of ``"click"``, ``"hover"``,
        ``"selected"`` and ``"relayout"`` (one name or an iterable of names). Each arrives
        as ``input.<id>_<event>``, namespaced like the output inside a module.
        ``click``, ``hover`` and ``selected`` carry ``{"points": [...]}`` where each point
        holds plotly's scalar fields (``curveNumber``, ``pointNumber``, ``pointIndex``,
        ``x``, ``y``, ``z``, ``text``, ``label``, ``value``, ...) plus ``customdata``,
        ``bbox`` and ``pointNumbers`` when present; a box or lasso selection adds ``range``
        or ``lassoPoints``. ``relayout`` carries plotly's relayout data as is (zoom and pan
        ranges, ``autorange``, ``dragmode``; a resize reports ``{"autosize": true}``).
        A click fires on every click, repeated or not; hover is debounced and becomes
        ``None`` once the pointer leaves the graph; a double-click deselect sets
        ``selected`` to ``None``.
    max_event_points
        The most points one event carries, 10 000 by default. An event with more points
        (a box or lasso over a dense trace) arrives with ``"points": None`` and
        ``"point_count"`` set, plus its ``range`` or ``lassoPoints`` as usual, so the
        selection is known and membership can be recomputed on the server, where the
        data is. At 10 000 points an event is about 1 MB; ``None`` lifts the cap, and a
        selection of 150 000 points or more then exceeds the 16 MB websocket message
        limit uvicorn applies by default, which closes the session.
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
        events: str | Iterable[str] | None = None,
        max_event_points: int | None = DEFAULT_MAX_EVENT_POINTS,
    ) -> None:
        self.height = height
        self.width = width
        self.figurewidget_margins = figurewidget_margins
        self.config = config
        self.post_script = post_script
        self.events = normalize_events(events)
        self.max_event_points = normalize_max_event_points(max_event_points)
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
            "events": list(self.events),
            "max_event_points": self.max_event_points,
        }
