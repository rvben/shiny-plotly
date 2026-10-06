"""
The per-session state of outputs rendered with ``render_plotly(resample=...)``, and the
guard that keeps in-place updates from undoing what resampling relies on.

Kept apart from ``_resample`` so the in-place updates can consult it without importing
numpy, which shiny-plotly does not require unless resampling is used.
"""

from __future__ import annotations

import functools
import weakref
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from shiny.session import Session

# Name suffix of the input through which the browser reports the view of a resampled output.
VIEW_INPUT_SUFFIX = "__shiny_plotly_view"

# Trace attributes that link a trace to others on its subplot or decide which subplot and
# trace type it is. Changing one on any trace of a resampled output could make a trace
# plotly fills or stacks against a sampled one, so they are refused on every trace.
_LINKING = frozenset({"type", "stackgroup", "fill", "xaxis", "yaxis"})

# Attributes a sample depends on, refused on a resampled trace on top of its per-point ones.
_SAMPLED = _LINKING | {
    "selectedpoints",
    "connectgaps",
    "xcalendar",
    "xperiod",
    "xperiod0",
    "xperiodalignment",
    "error_x",
    "error_y",
}

# arrayOk or data-array attributes that are not one value per point: meta is referenced
# as a whole from templates, the colorbar ticks label an axis, and a scatter fill takes
# one pattern for the whole fill.
_NOT_PER_POINT = ("meta", "marker.colorbar.", "fillpattern.")


class Sampled(Protocol):
    """What the guard needs to know of a resampled render."""

    trace_count: int

    @property
    def axes(self) -> Mapping[int, str]:
        """The x axis name (``"x"``, ``"x2"``) of each resampled trace, by trace index."""
        ...

    @property
    def y_axes(self) -> Mapping[int, str]:
        """The y axis name of each resampled trace, by trace index."""
        ...

    @property
    def per_point(self) -> Mapping[int, frozenset[str]]:
        """The dotted paths each resampled trace holds one value per point of, by index."""
        ...


@dataclass
class OutputView:
    """One resampled output in one session: its render revision and the data behind it."""

    revision: int = 0
    sampled: Sampled | None = None
    effect: Any = field(default=None, repr=False)


_outputs: MutableMapping[Session, dict[str, OutputView]] = weakref.WeakKeyDictionary()


def output_view(session: Session, name: str) -> OutputView:
    """The state of output ``name`` (namespaced) in ``session``, created on first use."""
    root = session.root_scope()
    views = _outputs.get(root)
    if views is None:
        views = _outputs[root] = {}

        # The full data of every resampled trace goes with the session, not with the
        # garbage collector's view of it.
        def forget() -> None:
            _outputs.pop(root, None)

        root.on_ended(forget)
    return views.setdefault(name, OutputView())


def sampled_output(session: Session, name: str) -> Sampled | None:
    views = _outputs.get(session.root_scope())
    view = views.get(name) if views else None
    return view.sampled if view else None


@functools.cache
def per_point_attributes() -> frozenset[str]:
    """
    Dotted paths of every scatter attribute that can hold one value per point.

    Derived from plotly's own validators (data arrays and ``arrayOk`` attributes), so an
    attribute a later plotly adds is covered without a list here to keep up to date.
    """
    import plotly.graph_objects as go
    from _plotly_utils.basevalidators import (
        CompoundValidator,
        DataArrayValidator,
    )

    def walk(obj: Any, prefix: str) -> list[str]:
        paths: list[str] = []
        for prop in obj._valid_props:
            path = prefix + prop
            # Skip excluded attributes before reading validators: older plotly versions
            # retain deprecated colorbar properties whose validator classes no longer exist.
            if any(path == cut or path.startswith(cut) for cut in _NOT_PER_POINT):
                continue
            validator = obj._get_validator(prop)
            if isinstance(validator, CompoundValidator):
                paths += walk(validator.data_class(), path + ".")
            elif isinstance(validator, DataArrayValidator) or getattr(validator, "array_ok", False):
                paths.append(path)
        return paths

    found = set(walk(go.Scatter(), "")) | set(walk(go.Scattergl(), ""))
    return frozenset(found)


def attribute_paths(update: Mapping[str, Any]) -> list[str]:
    """
    The attribute paths a restyle sets: ``{"marker": {"color": ...}}`` and
    ``{"marker.color": ...}`` both name ``marker.color``; ``{"marker": ...}`` alone names
    ``marker``, which reaches everything under it.
    """
    return [path for path, _, _ in _leaves(update)]


def _leaves(update: Mapping[str, Any]) -> list[tuple[str, Any, bool]]:
    """Each path a restyle sets, its value, and whether that value sat inside a dict."""
    found: list[tuple[str, Any, bool]] = []
    for key, value in update.items():
        path = str(key).split("[", 1)[0]
        if isinstance(value, Mapping) and value:
            found += [(f"{path}.{sub}", leaf, True) for sub, leaf, _ in _leaves(value)]
        else:
            found.append((path, value, False))
    return found


def _is_array(value: Any) -> bool:
    return hasattr(value, "__len__") and not isinstance(value, (str, bytes, Mapping))


def _trace_value(value: Any, position: int, nested: bool) -> Any:
    """
    What a restyle sets on the trace at ``position`` of its indices: plotly.js hands the
    traces a top-level list's items in turn. A list inside a dict is kept as it is, so
    one there is taken as per-point values.
    """
    if not nested and _is_array(value) and len(value):
        return value[position % len(value)]
    return value


def _reaches(path: str, attributes: frozenset[str] | set[str]) -> bool:
    """Whether setting ``path`` sets one of ``attributes``, itself or a container of it."""
    return any(
        path == a or a.startswith(path + ".") or path.startswith(a + ".") for a in attributes
    )


def _trace_indices(indices: Sequence[int] | None, count: int) -> list[int]:
    if indices is None:
        return list(range(count))
    return [index + count if index < 0 else index for index in indices]


def _refuse(name: str, what: str) -> None:
    raise ValueError(
        f"output {name!r} is rendered with resample=, so {what} would put the full data "
        "and the drawn sample out of step; re-render the output instead"
    )


def _check_restyle(
    name: str, sampled: Sampled, update: Mapping[str, Any], indices: Sequence[int] | None
) -> None:
    leaves = _leaves(update)
    for path, _, _ in leaves:
        if _reaches(path, set(_LINKING)):
            _refuse(name, f"restyling {path!r}")
    per_point = per_point_attributes()
    for position, index in enumerate(_trace_indices(indices, sampled.trace_count)):
        carried = sampled.per_point.get(index)
        if carried is None:
            continue
        for path, value, nested in leaves:
            # A path the trace holds per point is redrawn from the full data at every
            # view, and a new per-point array would be drawn against the sample.
            if _reaches(path, carried | _SAMPLED) or (
                path in per_point and _is_array(_trace_value(value, position, nested))
            ):
                _refuse(name, f"restyling {path!r} of resampled trace {index}")


def check_update(session: Session, name: str, method: str, args: Sequence[Any]) -> None:
    """Raise ValueError when an in-place update would break the sample drawn in ``name``."""
    sampled = sampled_output(session, name)
    if sampled is None or not sampled.axes:
        return
    if method in ("extendTraces", "prependTraces", "addTraces", "deleteTraces"):
        _refuse(name, f"{method} (which adds, removes or shifts points or traces)")
    relayout: Mapping[str, Any] = {}
    if method == "restyle":
        _check_restyle(name, sampled, args[0], args[1])
    elif method == "relayout":
        relayout = args[0]
    else:  # "update": [restyle, relayout(, indices)]
        _check_restyle(name, sampled, args[0], args[2] if len(args) > 2 else None)
        relayout = args[1]
    # An x axis's type and rangeslider decide what a sample can draw; a y axis's type
    # decides which values break the line (a log axis breaks it at zero and below).
    guarded_axes = {"xaxis" + a[1:]: {"type", "rangeslider"} for a in sampled.axes.values()}
    guarded_axes.update({"yaxis" + a[1:]: {"type"} for a in sampled.y_axes.values()})
    for path in attribute_paths(relayout):
        head, _, rest = path.partition(".")
        keys = guarded_axes.get(head)
        if keys is not None and (not rest or _reaches(rest, keys)):
            _refuse(name, f"relayout of {path!r}")
