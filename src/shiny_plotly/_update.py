"""
In-place updates to a drawn figure: plotly.js's extendTraces, restyle and relayout,
called from the server on the graph div a :class:`~shiny_plotly.render_plotly` output keeps.

A re-render sends the whole figure; these send only the change, which is what a live
dashboard wants for a new point every second. They ride on Shiny's custom messages and
are applied by the browser helper to the graph div of the named output. One sent while
the output has no figure drawn (its first render is still running, it sits in a hidden
tab, it shows an error or was emptied by ``None``) is held and applied, in order, right
after the output's next draw. A re-render replaces the figure, updates included, with
what the render function returns. All three are coroutines: await them from an
``async def`` effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from plotly.io.json import to_json_plotly
from shiny.session import Session, require_active_session

__all__ = ("extend_traces", "relayout", "restyle")

MESSAGE_TYPE = "shiny-plotly"

Indices = int | Sequence[int] | None


def _indices(indices: Indices) -> list[int] | None:
    if indices is None:
        return None
    if isinstance(indices, int):
        return [indices]
    return list(indices)


async def _send(id: str, method: str, args: Sequence[Any], session: Session | None) -> None:
    session = require_active_session(session)
    # Serialised by plotly, like the figure itself, so numpy arrays, pandas columns and
    # datetimes in the update are encoded the way plotly.js expects.
    message = {"id": session.ns(id), "method": method, "args": to_json_plotly(list(args))}
    await session.send_custom_message(MESSAGE_TYPE, message)


async def extend_traces(
    id: str,
    data: Mapping[str, Sequence[Sequence[Any]]],
    indices: Indices = None,
    *,
    max_points: int | None = None,
    session: Session | None = None,
) -> None:
    """
    Append points to traces of the figure in output ``id`` (``Plotly.extendTraces``).

    ``data`` maps an array attribute to one sequence of new values per trace, in the
    order of ``indices``: ``{"x": [[t]], "y": [[v]]}`` appends one point to one trace,
    ``{"y": [[1], [2]]}`` with ``indices=[0, 1]`` one point to each of two traces.
    ``indices`` defaults to every trace in the figure. ``max_points`` drops the oldest
    points past that many, for a rolling window.

    The update is sent to the session's client; outside a session it fails. Use it
    from a reactive effect, for instance one driven by ``reactive.invalidate_later``.
    """
    if max_points is not None and (not isinstance(max_points, int) or max_points < 1):
        raise ValueError(f"max_points must be a positive integer, got {max_points!r}")
    await _send(id, "extendTraces", [data, _indices(indices), max_points], session)


async def restyle(
    id: str,
    update: Mapping[str, Any],
    indices: Indices = None,
    *,
    session: Session | None = None,
) -> None:
    """
    Change trace attributes of the figure in output ``id`` (``Plotly.restyle``).

    ``update`` maps attribute paths to values: ``{"marker.color": "red"}`` applies to
    every trace in ``indices`` (default: all); a list value applies per trace, as in
    ``{"opacity": [0.5, 1]}`` with ``indices=[0, 1]``. Array attributes take a list of
    arrays, one per trace: ``{"y": [[1, 2, 3]]}``.
    """
    await _send(id, "restyle", [update, _indices(indices)], session)


async def relayout(
    id: str,
    update: Mapping[str, Any],
    *,
    session: Session | None = None,
) -> None:
    """
    Change the layout of the figure in output ``id`` (``Plotly.relayout``).

    ``update`` maps layout attribute paths to values: ``{"title.text": "Live"}``,
    ``{"xaxis.range": [0, 10]}``, ``{"xaxis.autorange": True}``. With the ``relayout``
    event enabled on the output, the browser reports the result back as
    ``input.<id>_relayout``, the same as a user's zoom.
    """
    await _send(id, "relayout", [update], session)
