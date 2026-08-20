"""
In-place updates to a drawn figure: plotly.js's extendTraces, prependTraces, addTraces,
deleteTraces, restyle, relayout and update, called from the server on the graph div a
:class:`~shiny_plotly.render_plotly` output keeps.

A re-render sends the whole figure; these send only the change, which is what a live
dashboard wants for a new point every second. They ride on Shiny's custom messages and
are applied by the browser helper to the graph div of the named output. One sent while
the output has no figure drawn (its first render is still running, it sits in a hidden
tab, it shows an error or was emptied by ``None``) is held and applied, in order, right
after the output's next draw. A re-render replaces the figure, updates included, with
what the render function returns. All of these are coroutines: await them from an
``async def`` effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from plotly.io.json import to_json_plotly
from shiny.session import Session, require_active_session

__all__ = (
    "add_traces",
    "delete_traces",
    "extend_traces",
    "prepend_traces",
    "relayout",
    "restyle",
    "update",
)

MESSAGE_TYPE = "shiny-plotly"

Indices = int | Sequence[int] | None

# One trace: a dict of trace attributes or a graph object such as ``go.Scatter``.
Trace = Mapping[str, Any] | Any


def _indices(indices: Indices) -> list[int] | None:
    if indices is None:
        return None
    if isinstance(indices, int):
        return [indices]
    return list(indices)


def _check_max_points(max_points: int | None) -> None:
    # bool is excluded by name: it is an int in Python but serializes to a JSON true,
    # which plotly.js reads as non-numeric and quietly treats as no cap at all.
    if max_points is not None and (
        isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1
    ):
        raise ValueError(f"max_points must be a positive integer, got {max_points!r}")


def _trace_json(trace: Trace) -> Any:
    to_json = getattr(trace, "to_plotly_json", None)
    return to_json() if callable(to_json) else trace


def _traces(traces: Trace | Sequence[Trace]) -> list[Any]:
    one = isinstance(traces, Mapping) or hasattr(traces, "to_plotly_json")
    out = [_trace_json(t) for t in ([traces] if one else list(traces))]
    if not out:
        raise ValueError("add_traces needs at least one trace")
    return out


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
    _check_max_points(max_points)
    await _send(id, "extendTraces", [data, _indices(indices), max_points], session)


async def prepend_traces(
    id: str,
    data: Mapping[str, Sequence[Sequence[Any]]],
    indices: Indices = None,
    *,
    max_points: int | None = None,
    session: Session | None = None,
) -> None:
    """
    Prepend points to traces of the figure in output ``id`` (``Plotly.prependTraces``).

    The mirror of :func:`extend_traces`: the new values go in front of the existing
    ones, and ``max_points`` drops the newest points past that many instead of the
    oldest. Same ``data`` and ``indices`` shapes.
    """
    _check_max_points(max_points)
    await _send(id, "prependTraces", [data, _indices(indices), max_points], session)


async def add_traces(
    id: str,
    traces: Trace | Sequence[Trace],
    indices: Indices = None,
    *,
    session: Session | None = None,
) -> None:
    """
    Add whole traces to the figure in output ``id`` (``Plotly.addTraces``).

    ``traces`` is one trace or a sequence of traces, each a graph object
    (``go.Scatter(y=[1, 2])``) or a dict of trace attributes (``{"y": [1], "type":
    "bar"}``). ``indices`` says where each lands in the figure's trace order; without
    it they are appended. The added traces count from then on for :func:`restyle`,
    :func:`extend_traces` and :func:`delete_traces` indices.
    """
    args: list[Any] = [_traces(traces)]
    if indices is not None:
        args.append(_indices(indices))
    await _send(id, "addTraces", args, session)


async def delete_traces(
    id: str,
    indices: int | Sequence[int],
    *,
    session: Session | None = None,
) -> None:
    """
    Remove traces from the figure in output ``id`` (``Plotly.deleteTraces``).

    ``indices`` names the traces to remove, by position in the figure's trace order
    (an int or a sequence of ints; negatives count from the end). The remaining traces
    renumber, exactly as a Python list does on ``del``.
    """
    if indices is None:
        raise ValueError("delete_traces needs the indices of the traces to remove")
    await _send(id, "deleteTraces", [_indices(indices)], session)


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


async def update(
    id: str,
    restyle: Mapping[str, Any] | None = None,
    relayout: Mapping[str, Any] | None = None,
    indices: Indices = None,
    *,
    session: Session | None = None,
) -> None:
    """
    Change trace attributes and the layout of the figure in output ``id`` in one
    redraw (``Plotly.update``).

    ``restyle`` takes what :func:`restyle` takes and ``relayout`` what
    :func:`relayout` takes; at least one is required. ``indices`` scopes the trace
    part, like the ``indices`` of :func:`restyle`. One call means one redraw, where
    separate :func:`restyle` and :func:`relayout` calls would draw twice.
    """
    if restyle is None and relayout is None:
        raise ValueError("update needs restyle or relayout (or both)")
    args: list[Any] = [restyle or {}, relayout or {}]
    if indices is not None:
        args.append(_indices(indices))
    await _send(id, "update", args, session)
