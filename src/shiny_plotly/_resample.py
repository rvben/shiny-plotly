"""
Server-side resampling for ``render_plotly(resample=...)``: long line traces travel as a
min-max sample of a few thousand points, and every zoom or pan brings a fresh sample of
just the range in view, sliced from the full data the session keeps.

Imported only when an output asks for resampling, since it needs numpy and shiny-plotly
does not otherwise.
"""

from __future__ import annotations

import base64
import datetime
import math
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from plotly.io.json import to_json_plotly
from shiny import reactive
from shiny.module import ResolvedId
from shiny.session import Session, session_context

from ._extrema import ExtremaIndex
from ._views import VIEW_INPUT_SUFFIX, attribute_paths, output_view, per_point_attributes

# Name of the in-place update method that carries a view's sample to the browser.
RESAMPLE_METHOD = "resample"

# plotly 6 carries numpy arrays in a figure dict as {"dtype", "bdata"[, "shape"]}.
_DTYPES: dict[str, Any] = {
    "i1": np.int8,
    "u1": np.uint8,
    "u1c": np.uint8,
    "i2": np.int16,
    "u2": np.uint16,
    "i4": np.int32,
    "u4": np.uint32,
    "f4": np.float32,
    "f8": np.float64,
}

# Axis types on which an x value is a position on a continuous scale.
_CONTINUOUS_AXES = (None, "-", "linear", "date", "log")


def sample(y: Any, budget: int, *, connectgaps: bool = False) -> np.ndarray:
    """
    Indices into ``y`` that draw the same picture as all of ``y`` with at most ``budget``
    finite points.

    The first and last index are always kept. The interior is split into buckets of
    equal index width, and each keeps the index of its smallest and its largest finite
    value, so no spike or dip is lost. A non-finite value (NaN or an infinity, which
    plotly's encoder sends as null) is a gap in the line: between two kept indices with
    any gap in the data between them, the first gap's index is kept too, so the sample
    breaks the line exactly where the data does. ``connectgaps`` traces draw across
    gaps, so they need no gap indices. Sorted, without repeats; every index when ``y``
    has no more than ``budget`` values.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n <= budget:
        return np.arange(n)
    finite = np.isfinite(y)
    all_finite = bool(finite.all())
    if not all_finite and not finite.any():
        # Missing series have no extrema or gap boundaries to preserve between finite
        # points. Retain the same endpoints without reductions or enumerating every gap.
        return np.array([0, n - 1])
    interior = y[1:-1]
    # Equal buckets of `width` values: vectorised reductions instead of a Python loop
    # over buckets. Finite data needs no full-size work buffers or masked copies.
    width = -(-len(interior) // ((budget - 2) // 2))
    count = -(-len(interior) // width)
    rows = np.arange(count)
    if all_finite:
        # Full buckets are views; reduce the partial final bucket separately so it
        # needs no padding. argmin/argmax keep the first occurrence of tied extrema.
        full = len(interior) // width
        buckets = interior[: full * width].reshape(full, width)
        low_at = np.empty(count, dtype=np.intp)
        high_at = np.empty(count, dtype=np.intp)
        low_at[:full] = buckets.argmin(axis=1)
        high_at[:full] = buckets.argmax(axis=1)
        if full < count:
            tail = interior[full * width :]
            low_at[-1], high_at[-1] = tail.argmin(), tail.argmax()
        has_value = np.ones(count, dtype=bool)
    else:
        # Reuse one buffer for both reductions, changing only gaps and padding from
        # +inf to -inf between them. The original values are never written to.
        inside = finite[1:-1]
        work = np.full(count * width, np.inf)
        np.copyto(work[: len(interior)], interior, where=inside)
        buckets = work.reshape(count, width)
        low_at = buckets.argmin(axis=1)
        # An all-gap bucket contributes no finite extrema; its gap is found below.
        has_value = np.isfinite(buckets[rows, low_at])
        work[: len(interior)][~inside] = -np.inf
        work[len(interior) :] = -np.inf
        high_at = buckets.argmax(axis=1)
    starts = 1 + rows * width
    extremes = np.concatenate([(starts + low_at)[has_value], (starts + high_at)[has_value]])
    kept = np.unique(np.concatenate([[0, n - 1], extremes]))
    if connectgaps or all_finite:
        return kept
    gaps = np.flatnonzero(~finite)
    # The first gap after each kept index, where it comes before the next kept index and
    # both are finite: a line only runs between two finite points.
    after = np.searchsorted(gaps, kept[:-1], side="right")
    has_gap = after < len(gaps)
    first = gaps[np.minimum(after, len(gaps) - 1)]
    between = has_gap & (first < kept[1:]) & finite[kept[:-1]] & finite[kept[1:]]
    return np.union1d(kept, first[between])


class Ineligible(Exception):
    """Why one trace cannot be resampled; it is then sent whole."""


def as_array(value: Any) -> np.ndarray:
    """A figure dict's array value as numpy, whether given as a list, numpy or bdata."""
    if isinstance(value, Mapping) and isinstance(value.get("bdata"), str):
        dtype = _DTYPES.get(str(value.get("dtype")))
        if dtype is None:
            raise Ineligible(f"its data has an unknown binary dtype {value.get('dtype')!r}")
        flat = np.frombuffer(base64.b64decode(value["bdata"]), dtype=dtype)
        shape = value.get("shape")
        if shape is None:
            return flat
        dims = str(shape).split(",") if isinstance(shape, str) else shape
        return flat.reshape([int(d) for d in dims])
    if isinstance(value, np.ndarray):
        return value
    try:
        return np.asarray(value)
    except ValueError:
        # Rows of different lengths (per-point customdata, say): one object per point.
        out = np.empty(len(value), dtype=object)
        out[:] = list(value)
        return out


def _wall_clock(value: Any) -> np.datetime64:
    """One date as numpy, at the wall-clock time plotly.js draws; it ignores UTC offsets."""
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return np.datetime64(value, "us")


def axis_coordinates(x: np.ndarray, *, copy: bool = True) -> np.ndarray:
    """
    ``x`` in the unit plotly.js computes with on its axis: numbers as they are, dates as
    milliseconds since the epoch of their wall-clock time.
    """
    if x.dtype.kind in "iuf":
        return x.astype(float, copy=copy)
    if x.dtype.kind == "M":
        if np.isnat(x).any():
            raise Ineligible("x holds a missing date (NaT)")
        # A microsecond array already holds epoch integers. Reinterpret that
        # buffer instead of copying it twice; division still owns the float
        # coordinates, so even a caller-owned or read-only x stays untouched.
        return x.astype("datetime64[us]", copy=False).view(np.int64) / 1000.0
    if x.dtype.kind == "O" and all(isinstance(v, (datetime.date, np.datetime64)) for v in x):
        # Pandas is already loaded when an app supplies Timestamp objects. Do not
        # import it just to sample stdlib dates, or make it a required dependency.
        pandas = sys.modules.get("pandas")
        if pandas is not None and not pandas.__version__.startswith(("0.", "1.")):
            zone = getattr(x[0], "tzinfo", None) if len(x) else None
            if (zone is None or type(zone) is datetime.timezone) and all(
                getattr(value, "tzinfo", None) == zone for value in x
            ):
                local = x  # naive or a single fixed offset cannot normalize a DST gap
            else:
                # Strip each offset before pandas sees it. Even Timestamp can hold a
                # nonexistent local time when built from a stdlib aware datetime.
                local = [
                    value.replace(tzinfo=None)
                    if isinstance(value, datetime.datetime) and value.tzinfo is not None
                    else value
                    for value in x
                ]
            try:
                dates = (
                    pandas.DatetimeIndex(local).tz_localize(None).to_numpy(dtype="datetime64[us]")
                )
            except (TypeError, ValueError, OverflowError):
                # Mixed timezones and dates outside pandas' range keep the existing
                # element-wise wall-clock conversion, without UTC normalization.
                pass
            else:
                return axis_coordinates(dates)
        return axis_coordinates(np.array([_wall_clock(v) for v in x], dtype="datetime64[us]"))
    raise Ineligible("x is not numbers or dates")


def _get(node: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _set(trace: dict[str, Any], path: str, value: Any) -> None:
    """Set ``path`` in ``trace``, copying each container on the way, so the caller's
    figure is never written to."""
    keys = path.split(".")
    node = trace
    for key in keys[:-1]:
        node[key] = dict(node[key])
        node = node[key]
    node[keys[-1]] = value


def _x_axis(trace: Mapping[str, Any]) -> str:
    return str(trace.get("xaxis") or "x")


def _y_axis(trace: Mapping[str, Any]) -> str:
    return str(trace.get("yaxis") or "y")


def _axis_layout(layout: Mapping[str, Any], axis: str) -> dict[str, Any]:
    """The layout of axis ``axis`` (``"x"``, ``"y2"``), the figure's own values over its
    template's."""
    key = axis[0] + "axis" + axis[1:]
    template = _get(layout, "template.layout." + key) or {}
    return {**template, **(layout.get(key) or {})}


def _is_categorical(x: Any) -> bool:
    """Whether ``x`` holds text or nested lists, either of which can make plotly type its
    axis as a category or multicategory axis."""
    if x is None or isinstance(x, Mapping):
        return False  # absent, or binary and so numbers
    if isinstance(x, np.ndarray) and x.dtype.kind != "O":
        return x.dtype.kind in "US" or x.ndim > 1
    return any(isinstance(v, (str, bytes, list, tuple, np.ndarray)) for v in x)


def _has_dates(value: Any) -> bool:
    """A conservative date-axis hint for native inputs, without copying arrays."""
    if isinstance(value, np.ndarray):
        value = value.flat
    return (
        isinstance(value, (list, tuple, np.flatiter))
        and len(value) > 0
        and isinstance(
            next((v for v in value if v is not None), None), (datetime.date, np.datetime64)
        )
    )


@dataclass
class Series:
    """The full data of one resampled trace."""

    axis: str
    coords: np.ndarray
    y: np.ndarray
    arrays: dict[str, np.ndarray]
    connectgaps: bool
    selected: np.ndarray | None
    y_axis: str = "y"
    broad_views: int = 0
    index_attempted: bool = False
    extrema: ExtremaIndex | None = None

    def pick(self, budget: int, span: tuple[float, float] | None) -> np.ndarray:
        """The indices to draw for a view of ``span`` on the axis (``None``: everything),
        reaching one point past each edge, so the line runs out of view on both sides."""
        start, stop = 0, len(self.coords)
        if span is not None:
            lo, hi = min(span), max(span)
            start = max(int(np.searchsorted(self.coords, lo, side="left")) - 1, 0)
            stop = min(int(np.searchsorted(self.coords, hi, side="right")) + 1, stop)
            # An index pays for itself only after repeated broad views. Keep first
            # draws and narrow zooms on the direct sampler; a new render resets this
            # state. Dense gaps decline the index and are not retried.
            if stop - start >= max(1_000_000, budget * 1024):
                self.broad_views += 1
                if self.broad_views >= 3 and not self.index_attempted:
                    self.index_attempted = True
                    self.extrema = ExtremaIndex.build(self.y)
                if self.extrema is not None:
                    return self.extrema.pick(start, stop, budget, connectgaps=self.connectgaps)
        return start + sample(self.y[start:stop], budget, connectgaps=self.connectgaps)

    def attributes(self, kept: np.ndarray) -> dict[str, Any]:
        """The trace's per-point attributes at the ``kept`` indices, by dotted path."""
        out: dict[str, Any] = {path: values[kept] for path, values in self.arrays.items()}
        if self.selected is not None:
            out["selectedpoints"] = np.flatnonzero(np.isin(kept, self.selected)).tolist()
        return out


@dataclass
class Record:
    """The resampled traces of one render of one output."""

    budget: int
    trace_count: int
    series: dict[int, Series] = field(default_factory=dict)
    # Each trace's full-range sample, drawn first and again whenever the view is reset.
    overview: dict[int, np.ndarray] = field(default_factory=dict)

    def pick(self, index: int, span: tuple[float, float] | None) -> np.ndarray:
        if span is None:
            return self.overview[index]
        return self.series[index].pick(self.budget, span)

    @property
    def axes(self) -> dict[int, str]:
        return {index: s.axis for index, s in self.series.items()}

    @property
    def y_axes(self) -> dict[int, str]:
        return {index: s.y_axis for index, s in self.series.items()}

    @property
    def per_point(self) -> dict[int, frozenset[str]]:
        return {index: frozenset(s.arrays) for index, s in self.series.items()}


def _numbers(values: np.ndarray, what: str, *, copy: bool = True) -> np.ndarray:
    if values.dtype.kind in "iuf":
        return values.astype(float, copy=copy)
    if values.dtype.kind == "O":
        try:
            return values.astype(float)  # None becomes NaN, a gap, as plotly draws it
        except (TypeError, ValueError):
            pass
    raise Ineligible(f"{what} is not numbers")


def _series(
    trace: Mapping[str, Any], budget: int, log_y: bool, *, owned_arrays: bool
) -> Series | None:
    """The full data of ``trace``, or None when it is short enough to send whole."""
    if trace.get("y") is None:
        return None
    y = as_array(trace["y"])
    if y.ndim != 1 or len(y) <= budget:
        return None
    n = len(y)
    if trace.get("x") is None:
        # plotly places the points at x0 + i * dx; the sample names its x explicitly.
        x0, dx = trace.get("x0", 0), trace.get("dx", 1)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x0, dx)):
            raise Ineligible("it has no x and its x0 or dx is not a number")
        x = coords = x0 + dx * np.arange(n, dtype=float)
    else:
        x = as_array(trace["x"])
        if x.ndim != 1 or len(x) != n:
            raise Ineligible("its x and y differ in length")
        coords = axis_coordinates(x, copy=not owned_arrays)
    if not np.isfinite(coords).all():
        raise Ineligible("x holds a missing or infinite value")
    if (coords[1:] < coords[:-1]).any():
        raise Ineligible("x is not sorted (in the wall-clock time plotly.js draws)")
    # Figure component serialization already copied its arrays. Reuse those snapshots,
    # but retain independent coordinates/numbers for caller-owned dictionary arrays.
    # Log masking always needs a separate writable copy, including for owned snapshots.
    y_numbers = _numbers(y, "y", copy=not owned_arrays or log_y)
    if log_y:
        # A log axis cannot place a value at or below zero, so plotly breaks the line
        # there, the same as at a NaN.
        y_numbers[y_numbers <= 0] = np.nan
    arrays = {"x": x, "y": y}
    for path in per_point_attributes() - {"x", "y"}:
        value = _get(trace, path)
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            continue
        values = as_array(value)
        if values.ndim >= 1 and len(values) == n:
            arrays[path] = values
    selected = trace.get("selectedpoints")
    return Series(
        axis=_x_axis(trace),
        coords=coords,
        y=y_numbers,
        arrays=arrays,
        connectgaps=bool(trace.get("connectgaps")),
        selected=None if selected is None else as_array(selected).astype(np.int64).ravel(),
    )


def _check_trace(
    trace: Mapping[str, Any],
    layout: Mapping[str, Any],
    fill_linked: set[tuple[str, str]],
    text_axes: set[str],
    local_axis_paths: list[str],
) -> None:
    """Raise Ineligible when a sample of ``trace`` would draw something the data does not."""
    if trace.get("stackgroup"):
        raise Ineligible("it is stacked, and stacking adds up values at shared x")
    subplot = (_x_axis(trace), _y_axis(trace))
    if subplot in fill_linked:
        raise Ineligible("a trace on its subplot fills to another (fill='tonext...')")
    if any(trace.get(key) is not None for key in ("xperiod", "xperiod0", "xperiodalignment")):
        raise Ineligible("it sets xperiod, which draws points away from their x")
    if trace.get("xcalendar") not in (None, "gregorian"):
        raise Ineligible("its x uses a non-gregorian calendar")
    for bar in ("error_x", "error_y"):
        spec = trace.get(bar)
        if (
            isinstance(spec, Mapping)
            and spec.get("visible", True) is not False
            and (spec.get("array") is not None or spec.get("arrayminus") is not None)
        ):
            raise Ineligible(f"its {bar} varies per point, and a sample could drop the widest bar")
    if _axis_layout(layout, subplot[1]).get("type") not in _CONTINUOUS_AXES:
        raise Ineligible("its y axis is not a continuous one")
    axis = _axis_layout(layout, subplot[0])
    if axis.get("type") not in _CONTINUOUS_AXES:
        raise Ineligible(f"its x axis is of type {axis.get('type')!r}")
    if axis.get("type") in (None, "-") and subplot[0] in text_axes:
        raise Ineligible("its x axis also carries text x, which can make it a category axis")
    slider = axis.get("rangeslider")
    if isinstance(slider, Mapping) and slider and slider.get("visible", True) is not False:
        raise Ineligible("its x axis has a rangeslider, which would show only the sample")
    guarded = {
        "xaxis" + subplot[0][1:]: {"type", "rangeslider"},
        "yaxis" + subplot[1][1:]: {"type"},
    }
    for path in local_axis_paths:
        head, _, rest = path.partition(".")
        if head in guarded and (not rest or rest.split(".")[0] in guarded[head]):
            raise Ineligible("local controls can change its axis type or rangeslider")


def resample_figure(
    fig_dict: dict[str, Any], budget: int, *, owned_arrays: bool = False
) -> tuple[dict[str, Any], Record]:
    """
    ``fig_dict`` with each long eligible trace replaced by a full-range sample, and the
    record of the full data behind them. A long trace that cannot be resampled is sent
    whole, with a warning that names it and says why.

    ``owned_arrays`` is reserved for the renderer's copied Figure components: their
    native arrays can serve as the record's snapshots without another conversion copy.
    """
    data = list(fig_dict.get("data") or [])
    layout = fig_dict.get("layout") or {}
    record = Record(budget=budget, trace_count=len(data))
    # Plotly's own controls can replace data without the server's update guard. The
    # sample and original-index maps would then describe a different trace entirely.
    local_controls = [
        item
        for key, children in (("updatemenus", "buttons"), ("sliders", "steps"))
        for control in layout.get(key, [])
        for item in control.get(children, [])
    ]
    local_data = any(
        item.get("method", "restyle") in ("restyle", "update", "animate") for item in local_controls
    )
    local_axis_paths = [
        path
        for item in local_controls
        if item.get("method") == "relayout"
        for args in (item.get("args"), item.get("args2"))
        if isinstance(args, (list, tuple)) and args
        for path in (
            attribute_paths(args[0])
            if isinstance(args[0], Mapping)
            else attribute_paths({args[0]: None})
            if isinstance(args[0], str)
            else []
        )
    ]
    # plotly links a tonext fill to the previous trace of the same subplot, wherever it
    # sits in the figure, so every trace on such a subplot is a possible fill partner.
    fill_linked = {
        (_x_axis(t), _y_axis(t)) for t in data if str(t.get("fill") or "").startswith("tonext")
    }
    text_axes = {_x_axis(t) for t in data if _is_categorical(t.get("x"))}
    date_axes = {
        _x_axis(t)
        for t in data
        if _has_dates(t.get("x")) or isinstance(t.get("x0"), (str, datetime.date, np.datetime64))
    }
    refused: list[str] = []
    for index, trace in enumerate(data):
        if trace.get("type", "scatter") not in ("scatter", "scattergl"):
            continue
        try:
            log_y = _axis_layout(layout, _y_axis(trace)).get("type") == "log"
            series = _series(trace, budget, log_y, owned_arrays=owned_arrays)
            if series is None:
                continue
            if fig_dict.get("frames") or local_data:
                raise Ineligible("animation frames or local controls can replace its data")
            _check_trace(trace, layout, fill_linked, text_axes, local_axis_paths)
            axis_type = _axis_layout(layout, series.axis).get("type")
            if series.arrays["x"].dtype.kind in "iuf" and (
                axis_type == "date" or (axis_type in (None, "-") and series.axis in date_axes)
            ):
                # Plotly interprets numeric date data in the browser's local
                # timezone, unlike date strings. Server coordinates cannot
                # predict its offsets (which can change at DST boundaries).
                raise Ineligible("numeric date x uses the browser's local timezone")
            series.y_axis = _y_axis(trace)
        except Ineligible as reason:
            refused.append(f"trace {index} ({reason})")
            continue
        record.series[index] = series
        record.overview[index] = kept = series.pick(budget, None)
        sampled = dict(trace)
        for path, values in series.attributes(kept).items():
            _set(sampled, path, values)
        data[index] = sampled
    if refused:
        warnings.warn(
            f"render_plotly(resample={budget}) sends these traces whole: " + "; ".join(refused),
            stacklevel=2,
        )
    return {**fig_dict, "data": data}, record


def full_index_maps(record: Record) -> dict[str, list[int]]:
    """Each resampled trace's full-range sample as indices into its data."""
    return {str(index): kept.tolist() for index, kept in record.overview.items()}


def _span(value: Any) -> tuple[float, float] | None:
    """A reported axis range as two finite floats; None for the full range."""
    # Shiny hands a JSON array over as a tuple.
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) else None


def view_update(record: Record, report: Any) -> dict[str, Any] | None:
    """
    For a view the browser reported, the sample of each resampled trace on a reported
    axis, by trace index: ``{"attributes": {path: values}, "index_map": [...]}``. None
    when the report names none of those axes.
    """
    axes = report.get("axes") if isinstance(report, Mapping) else None
    if not isinstance(axes, Mapping):
        return None
    traces: dict[str, Any] = {}
    for index, series in record.series.items():
        if series.axis in axes:
            kept = record.pick(index, _span(axes[series.axis]))
            traces[str(index)] = {
                "attributes": series.attributes(kept),
                "index_map": kept.tolist(),
            }
    return traces or None


def watch_view(session: Session, name: str) -> None:
    """
    Answer the view reports of output ``name`` with a sample of the range in view, for
    as long as the session lasts; one watcher per output, whichever render set it up.
    """
    root = session.root_scope()
    state = output_view(root, name)
    if state.effect is not None:
        return
    # Already namespaced ("mod-fig__..."), which a plain string key would be refused as.
    view_input = root.input[ResolvedId(name + VIEW_INPUT_SUFFIX)]

    with session_context(root):

        @reactive.effect
        @reactive.event(view_input)
        async def _answer_view() -> None:
            report = view_input()
            record = state.sampled
            if (
                not isinstance(record, Record)
                or not isinstance(report, Mapping)
                or report.get("revision") != state.revision
            ):
                return  # about a figure since re-rendered or emptied
            traces = view_update(record, report)
            if traces is None:
                return
            args = [state.revision, report.get("seq"), traces]
            message = {"id": name, "method": RESAMPLE_METHOD, "args": to_json_plotly(args)}
            await root.send_custom_message("shiny-plotly", message)

    state.effect = _answer_view
