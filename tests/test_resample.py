"""The min-max sampler behind render_plotly(resample=...), and the figures it samples."""

import base64
import datetime
import math
import warnings
from itertools import pairwise
from typing import Any

import numpy as np
import plotly.graph_objects as go
import pytest

from shiny_plotly._resample import as_array, resample_figure, sample, view_update


def drawn_segments(y: np.ndarray, indices: np.ndarray) -> list[tuple[int, int]]:
    """The line segments plotly draws through ``y[indices]``: consecutive finite pairs."""
    kept = y[indices]
    return [
        (int(indices[i]), int(indices[i + 1]))
        for i in range(len(indices) - 1)
        if np.isfinite(kept[i]) and np.isfinite(kept[i + 1])
    ]


def test_a_short_trace_is_kept_whole():
    y = np.arange(10.0)

    assert sample(y, budget=10).tolist() == list(range(10))


def test_the_sample_stays_within_budget_and_keeps_both_ends():
    y = np.random.default_rng(0).standard_normal(100_003)

    indices = sample(y, budget=1000)

    assert len(indices) <= 1000
    assert indices[0] == 0 and indices[-1] == len(y) - 1
    assert np.all(np.diff(indices) > 0), "sorted, no repeats"


@pytest.mark.parametrize("position", [1, 50_000, 99_998])
def test_a_single_spike_and_dip_survive(position):
    y = np.zeros(100_000)
    y[position] = 1e9
    y[position + 1 if position < 99_998 else position - 1] = -1e9

    kept = y[sample(y, budget=100)]

    assert kept.max() == 1e9 and kept.min() == -1e9


def test_every_bucket_contributes_its_extremes():
    """Each bucket's min and max are kept, so the envelope of the data is exact."""
    y = np.random.default_rng(1).standard_normal(10_000)
    indices = sample(y, budget=102)
    # (102 - 2) // 2 = 50 buckets over the 9998 interior values: 200 each, the last 198.
    interior = np.arange(1, len(y) - 1)
    buckets = [interior[i : i + 200] for i in range(0, len(interior), 200)]
    assert len(buckets) == 50

    for bucket in buckets:
        values = y[bucket]
        assert bucket[np.argmin(values)] in indices
        assert bucket[np.argmax(values)] in indices


@pytest.mark.parametrize("gap", [np.nan, np.inf, -np.inf])
def test_every_gap_in_a_bucket_stays_a_gap(gap):
    """[0, gap, 5, gap, -5]: plotly draws no line at all here, so neither may the sample."""
    y = np.zeros(1000)
    y[500:505] = [0, gap, 5, gap, -5]

    indices = sample(y, budget=10)
    segments = drawn_segments(y, indices)

    for a, b in segments:
        assert np.all(np.isfinite(y[a : b + 1])), f"segment {a}-{b} bridges a gap"


@pytest.mark.parametrize("sign", [1, -1])
def test_an_infinity_does_not_hide_a_finite_spike(sign):
    """plotly sends +-inf as null, a gap; the finite extreme is the one drawn."""
    y = np.zeros(1000)
    y[400], y[401] = sign * np.inf, sign * 10.0

    kept = y[sample(y, budget=10)]

    assert sign * 10.0 in kept


def test_connected_gaps_are_not_marked():
    y = np.zeros(1000)
    y[100::7] = np.nan

    indices = sample(y, budget=20, connectgaps=True)

    assert len(indices) <= 20
    assert np.all(np.isfinite(y[indices[1:-1]]))


def test_a_trace_that_is_all_gaps_samples_to_its_ends():
    y = np.full(1000, np.nan)

    assert sample(y, budget=10).tolist() == [0, 999]


@pytest.mark.parametrize("connect", [False, True])
@pytest.mark.parametrize("values", [[np.nan], [np.inf], [-np.inf], [np.nan, np.inf, -np.inf]])
def test_fully_missing_series_keep_endpoints_without_mutating_readonly_values(values, connect):
    y = np.resize(np.array(values), 1003)
    y.flags.writeable = False
    before = y.copy()

    assert sample(y, 102, connectgaps=connect).tolist() == [0, 1002]
    assert sample(y[:9], 10, connectgaps=connect).tolist() == list(range(9))
    np.testing.assert_array_equal(y, before)


def test_the_sample_draws_the_same_gaps_as_the_data():
    """Random gaps: every segment the sample draws is one the data draws, and every gap
    the data has between two kept points stays a gap."""
    rng = np.random.default_rng(2)
    y = rng.standard_normal(50_000)
    y[rng.integers(0, len(y), 500)] = np.nan

    indices = sample(y, budget=500)
    finite_points = np.isfinite(y[indices]).sum()

    assert finite_points <= 500
    for a, b in drawn_segments(y, indices):
        assert np.all(np.isfinite(y[a : b + 1]))


def test_five_million_points_sample_quickly():
    import time

    y = np.random.default_rng(3).standard_normal(5_000_000)
    start = time.perf_counter()
    sample(y, budget=2000)
    assert time.perf_counter() - start < 1.0


def reference_sample(y: np.ndarray, budget: int, connectgaps: bool) -> list[int]:
    """A slow bucket-by-bucket oracle, independent of the vectorised buffer handling."""
    if len(y) <= budget:
        return list(range(len(y)))
    width = math.ceil((len(y) - 2) / ((budget - 2) // 2))
    kept = {0, len(y) - 1}
    for start in range(1, len(y) - 1, width):
        valid = [i for i in range(start, min(start + width, len(y) - 1)) if math.isfinite(y[i])]
        if valid:
            kept.add(min(valid, key=lambda i: y[i]))
            kept.add(max(valid, key=lambda i: y[i]))
    if not connectgaps:
        ordered = sorted(kept)
        for left, right in pairwise(ordered):
            if math.isfinite(y[left]) and math.isfinite(y[right]):
                gap = next((i for i in range(left + 1, right) if not math.isfinite(y[i])), None)
                if gap is not None:
                    kept.add(gap)
    return sorted(kept)


@pytest.mark.parametrize("kind", ["random", "ties", "gaps", "infinities", "all_gaps", "end_gaps"])
@pytest.mark.parametrize("strides", ["contiguous", "column", "reversed"])
def test_sampler_matches_bucket_oracle_and_preserves_readonly_input(kind, strides):
    rng = np.random.default_rng(42)
    # Short inputs, exact bucket divisions, a partial last bucket, and odd budgets.
    for n in (0, 1, 9, 10, 101, 102, 103, 1003):
        values = rng.normal(size=n)
        if kind == "ties":
            values = np.resize(np.array([0.0, -0.0, 2.0, 2.0, -2.0, -2.0]), n)
        elif kind == "gaps":
            values[::7] = np.nan
        elif kind == "infinities":
            values[::3], values[1::5] = np.inf, -np.inf
        elif kind == "all_gaps":
            values[:] = np.nan
        elif kind == "end_gaps" and n:
            values[0], values[-1] = np.nan, np.inf
        if strides == "column":
            values = np.column_stack([values, values])[:, 0]
        elif strides == "reversed":
            values = values[::-1]
        values.flags.writeable = False
        before = values.copy()
        for budget in (10, 11, 102):
            for connect in (False, True):
                assert sample(values, budget, connectgaps=connect).tolist() == reference_sample(
                    values, budget, connect
                )
        np.testing.assert_array_equal(values, before)


# --- figures: which traces are resampled, and what they carry --------------------------

N = 1000
BUDGET = 50


def resampled(fig, budget: int = BUDGET):
    """The figure dict sent, the record kept, and any warning, for ``fig`` as rendered."""
    fig_dict = fig.to_dict() if isinstance(fig, go.Figure) else fig
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sent, record = resample_figure(fig_dict, budget)
    return sent, record, [str(w.message) for w in caught]


def line(n: int = N, **kwargs) -> go.Scatter:
    return go.Scatter(x=np.arange(n), y=np.sin(np.arange(n) / 10.0), **kwargs)


def test_a_long_trace_is_sampled_and_a_short_one_travels_whole():
    fig = go.Figure([line(), go.Scatter(x=[1, 2, 3], y=[4, 5, 6])])

    sent, record, warned = resampled(fig)

    assert list(record.series) == [0]
    assert len(sent["data"][0]["y"]) <= BUDGET
    assert sent["data"][1]["y"] == fig.to_dict()["data"][1]["y"]
    assert warned == []


@pytest.mark.parametrize("binary", [False, True])
def test_the_sample_is_the_data_at_the_kept_indices(binary):
    """Both native figure arrays and plotly 6 binary specs sample the original data."""
    x = np.arange(N) * 2.5
    y = np.random.default_rng(4).standard_normal(N)
    fig = go.Figure(go.Scattergl(x=x, y=y))
    fig_dict = fig.to_dict()
    if binary:
        for attribute, array in (("x", x), ("y", y)):
            fig_dict["data"][0][attribute] = {
                "dtype": "f8",
                "bdata": base64.b64encode(array.tobytes()).decode(),
            }

    sent, record, _ = resampled(fig_dict)
    kept = record.overview[0]

    assert np.array_equal(sent["data"][0]["x"], x[kept])
    assert np.array_equal(sent["data"][0]["y"], y[kept])
    assert kept[0] == 0 and kept[-1] == N - 1


def test_a_plain_list_figure_dict_is_sampled_too():
    trace = {"type": "scatter", "x": list(range(N)), "y": [float(i % 7) for i in range(N)]}
    fig = {"data": [trace]}

    sent, record, _ = resampled(fig)

    assert 0 in record.series
    assert len(sent["data"][0]["y"]) <= BUDGET


def test_the_callers_figure_is_not_written_to():
    fig = go.Figure(line(marker={"color": np.arange(N)}))
    fig_dict = fig.to_dict()
    before = fig_dict["data"][0]["marker"]["color"]
    before_y = fig_dict["data"][0]["y"]
    values = as_array(before_y).copy()

    resampled(fig_dict)

    assert fig_dict["data"][0]["marker"]["color"] is before
    assert fig_dict["data"][0]["y"] is before_y
    assert np.array_equal(as_array(before_y), values), "still the complete original data"


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("log_y", [False, True])
def test_owned_snapshots_can_be_reused_but_log_masking_never_writes_to_them(owned, log_y):
    x = np.arange(N, dtype=float)
    y = x - 10
    x.flags.writeable = y.flags.writeable = False
    fig = {"data": [{"x": x, "y": y}], "layout": {"yaxis": {"type": "log" if log_y else "linear"}}}

    _, record = resample_figure(fig, BUDGET, owned_arrays=owned)

    series = record.series[0]
    assert np.shares_memory(series.coords, x) == owned
    assert np.shares_memory(series.y, y) == (owned and not log_y)
    np.testing.assert_array_equal(y, x - 10)
    if log_y:
        assert np.isnan(series.y[:11]).all()
    else:
        np.testing.assert_array_equal(series.y, y)


def test_sortedness_validation_does_not_overflow_for_finite_extreme_coordinates():
    x = np.arange(N, dtype=float)
    x[0], x[-1] = -np.finfo(float).max, np.finfo(float).max
    # A subtraction-based sortedness check overflows even though these values are valid.
    x[1] = np.finfo(float).max / 2
    x[2:] = np.finfo(float).max

    _, record = resample_figure({"data": [{"x": x, "y": np.arange(N)}]}, BUDGET)

    assert 0 in record.series


def test_every_per_point_attribute_is_sliced_with_the_points():
    ids = np.arange(N)
    fig = go.Figure(
        line(
            customdata=ids,
            text=[f"p{i}" for i in ids],
            marker={"color": ids * 2, "size": np.full(N, 4)},
            hovertext=[f"h{i}" for i in ids],
            name="whole",
        )
    )

    sent, record, _ = resampled(fig)
    kept = record.overview[0]
    trace = sent["data"][0]

    assert list(trace["customdata"]) == kept.tolist()
    assert list(trace["text"]) == [f"p{i}" for i in kept]
    assert list(trace["hovertext"]) == [f"h{i}" for i in kept]
    assert list(trace["marker"]["color"]) == (kept * 2).tolist()
    assert len(trace["marker"]["size"]) == len(kept)
    assert trace["name"] == "whole"


def test_a_scalar_or_differently_sized_attribute_is_left_alone():
    fig = go.Figure(line(marker={"color": "red"}, customdata=[1, 2, 3]))

    sent, _, _ = resampled(fig)

    assert sent["data"][0]["marker"]["color"] == "red"
    assert list(sent["data"][0]["customdata"]) == [1, 2, 3]


def test_per_point_rows_of_different_lengths_are_sliced_as_rows():
    rows = [[i] * (1 + i % 3) for i in range(N)]
    fig = {"data": [{"x": list(range(N)), "y": [float(i) for i in range(N)], "customdata": rows}]}

    sent, record, _ = resampled(fig)

    assert [list(r) for r in sent["data"][0]["customdata"]] == [rows[i] for i in record.overview[0]]


def test_selectedpoints_is_remapped_to_positions_in_the_sample():
    fig = go.Figure(line(selectedpoints=[0, 5, N - 1]))

    sent, record, _ = resampled(fig)
    kept = record.overview[0]

    assert [int(kept[i]) for i in sent["data"][0]["selectedpoints"]] == [0, N - 1]


def test_a_trace_without_x_is_placed_at_x0_plus_dx_steps():
    fig = go.Figure(go.Scatter(y=np.arange(N, dtype=float), x0=10, dx=0.5))

    sent, record, _ = resampled(fig)
    kept = record.overview[0]

    assert np.array_equal(sent["data"][0]["x"], 10 + 0.5 * kept)
    assert np.array_equal(record.series[0].coords, 10 + 0.5 * np.arange(N))


def test_dates_are_placed_at_their_wall_clock_milliseconds():
    x = np.datetime64("2026-01-01T00:00") + np.arange(N) * np.timedelta64(1, "m")
    fig = go.Figure(go.Scatter(x=x, y=np.arange(N, dtype=float)))

    sent, record, _ = resampled(fig)

    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000
    assert record.series[0].coords[0] == start
    assert record.series[0].coords[1] - start == 60_000
    assert np.array_equal(sent["data"][0]["x"], x[record.overview[0]])


def test_timezone_aware_dates_are_placed_at_their_local_wall_clock():
    """plotly.js ignores offsets, so 09:00+02:00 is drawn at 09:00, and sliced there."""
    tz = datetime.timezone(datetime.timedelta(hours=2))
    start = datetime.datetime(2026, 6, 1, 9, 0, tzinfo=tz)
    x = [start + datetime.timedelta(minutes=i) for i in range(N)]
    fig = {"data": [{"x": x, "y": [float(i) for i in range(N)]}]}

    _, record, _ = resampled(fig)

    nine = datetime.datetime(2026, 6, 1, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000
    assert record.series[0].coords[0] == nine


def test_plain_python_dates_and_datetimes_are_placed_like_numpy_ones():
    day = datetime.date(2026, 1, 1)
    midnight = datetime.datetime(2026, 1, 1).replace(tzinfo=datetime.timezone.utc).timestamp()
    for x in (
        [day + datetime.timedelta(days=i) for i in range(N)],
        [datetime.datetime(2026, 1, 1) + datetime.timedelta(days=i) for i in range(N)],
    ):
        _, record, warned = resampled({"data": [{"x": x, "y": list(range(N))}]})
        assert warned == []
        assert record.series[0].coords[0] == midnight * 1000
        assert record.series[0].coords[1] - record.series[0].coords[0] == 86_400_000


def test_pandas_dates_with_a_timezone_are_placed_at_their_wall_clock():
    pd = pytest.importorskip("pandas")
    index = pd.date_range("2026-06-01 09:00", periods=N, freq="min", tz="Europe/Amsterdam")
    nine = datetime.datetime(2026, 6, 1, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000

    for x in (index, pd.Series(index)):
        _, record, warned = resampled(go.Figure(go.Scatter(x=x, y=np.arange(N, dtype=float))))
        assert warned == []
        assert record.series[0].coords[0] == nine


def test_zero_and_below_on_a_log_axis_are_gaps():
    y = np.ones(N)
    y[500] = 0.0
    fig = go.Figure(go.Scatter(x=np.arange(N), y=y)).update_yaxes(type="log")

    sent, record, _ = resampled(fig)

    assert np.isnan(record.series[0].y[500])
    assert 500 in record.overview[0], "the break in the line is kept"
    assert sent["data"][0]["y"][list(record.overview[0]).index(500)] == 0.0, "sent as given"


def test_on_a_linear_axis_zero_is_a_value():
    y = np.ones(N)
    y[500] = 0.0

    _, record, _ = resampled(go.Figure(go.Scatter(x=np.arange(N), y=y)))

    assert record.series[0].y[500] == 0.0


def test_a_missing_y_value_is_a_gap():
    y: list = [1.0] * N
    y[300] = None

    _, record, _ = resampled({"data": [{"x": list(range(N)), "y": y}]})

    assert 300 in record.overview[0]


def test_other_trace_types_are_never_resampled_or_warned_about():
    fig = go.Figure(go.Bar(x=np.arange(N), y=np.arange(N)))

    _, record, warned = resampled(fig)

    assert record.series == {} and warned == []


def test_a_trace_without_y_or_with_2d_y_is_left_alone():
    fig = {"data": [{"x": list(range(N))}, {"y": [[1.0, 2.0]] * N}]}

    _, record, warned = resampled(fig)

    assert record.series == {} and warned == []


REFUSED = {
    "stacked": (
        go.Figure([line(stackgroup="a"), line(stackgroup="a")]),
        "trace 0 (it is stacked",
    ),
    "fill partner": (
        go.Figure([line(), line(fill="tonexty")]),
        "trace 0 (a trace on its subplot fills to another",
    ),
    "fill partner earlier in the figure on its subplot": (
        go.Figure([line(), line(yaxis="y2"), line(fill="tonexty")]),
        "trace 0 (a trace on its subplot fills",
    ),
    "xperiod": (go.Figure(line(xperiod=5)), "it sets xperiod"),
    "calendar": (go.Figure(line(xcalendar="coptic")), "non-gregorian calendar"),
    "error bars": (
        go.Figure(line(error_y={"array": np.ones(N)})),
        "its error_y varies per point",
    ),
    "category x axis": (
        go.Figure(line()).update_xaxes(type="category"),
        "its x axis is of type 'category'",
    ),
    "category y axis": (
        go.Figure(line()).update_yaxes(type="category"),
        "its y axis is not a continuous one",
    ),
    "category axis from the template": (
        go.Figure(line()).update_layout(template={"layout": {"xaxis": {"type": "category"}}}),
        "its x axis is of type 'category'",
    ),
    "text on the same axis": (
        go.Figure([line(), go.Scatter(x=["a", "b"], y=[1, 2])]),
        "also carries text x",
    ),
    "rangeslider": (
        go.Figure(line()).update_xaxes(rangeslider_visible=True),
        "rangeslider",
    ),
    "unsorted x": (
        go.Figure(go.Scatter(x=np.arange(N)[::-1], y=np.arange(N))),
        "x is not sorted",
    ),
    "text x": (
        go.Figure(go.Scatter(x=[f"x{i}" for i in range(N)], y=np.arange(N))),
        "x is not numbers or dates",
    ),
    "NaN x": (
        go.Figure(go.Scatter(x=np.r_[np.nan, np.arange(N - 1.0)], y=np.arange(N))),
        "x holds a missing or infinite value",
    ),
    "NaT x": (
        {
            "data": [
                {
                    "x": np.r_[
                        np.datetime64("NaT", "D"),
                        np.datetime64("2026-01-01") + np.arange(N - 1).astype("timedelta64[D]"),
                    ],
                    "y": list(range(N)),
                }
            ]
        },
        "missing date (NaT)",
    ),
    "length mismatch": (
        go.Figure(go.Scatter(x=np.arange(N - 1), y=np.arange(N))),
        "x and y differ in length",
    ),
    "text y": (
        {"data": [{"x": list(range(N)), "y": [f"y{i}" for i in range(N)]}]},
        "y is not numbers",
    ),
    "text and missing y": (
        {"data": [{"x": list(range(N)), "y": [None, "a"] * (N // 2)}]},
        "y is not numbers",
    ),
    "text x0": (
        {"data": [{"y": list(range(N)), "x0": "2026-01-01"}]},
        "x0 or dx is not a number",
    ),
    "unknown binary dtype": (
        {"data": [{"x": list(range(N)), "y": {"dtype": "c16", "bdata": ""}}]},
        "unknown binary dtype 'c16'",
    ),
}


@pytest.mark.parametrize("case", REFUSED)
def test_a_trace_that_cannot_be_sampled_faithfully_is_sent_whole_with_the_reason(case):
    fig, reason = REFUSED[case]
    fig_dict = fig.to_dict() if isinstance(fig, go.Figure) else fig

    sent, record, warned = resampled(fig_dict)

    assert 0 not in record.series
    assert sent["data"][0] is fig_dict["data"][0], "sent whole, untouched"
    assert len(warned) == 1
    assert warned[0].startswith(f"render_plotly(resample={BUDGET}) sends these traces whole: ")
    assert reason in warned[0]


def test_one_warning_names_every_refused_trace():
    fig = go.Figure([line(xperiod=5), line(), line(xcalendar="coptic")])

    _, record, warned = resampled(fig)

    assert list(record.series) == [1]
    assert len(warned) == 1
    assert "trace 0 (it sets xperiod" in warned[0] and "trace 2 (its x uses" in warned[0]


def test_a_hidden_rangeslider_or_hidden_error_bars_do_not_refuse_a_trace():
    fig = go.Figure(line(error_y={"array": np.ones(N), "visible": False}))
    fig.update_xaxes(rangeslider={"visible": False})

    _, record, warned = resampled(fig)

    assert list(record.series) == [0] and warned == []


def test_explicit_continuous_axis_types_are_sampled():
    for axis_type in ("linear", "log", "-"):
        fig = go.Figure(line()).update_xaxes(type=axis_type)
        assert list(resampled(fig)[1].series) == [0], axis_type


def test_a_trace_on_its_own_axes_records_them():
    fig = go.Figure(line(xaxis="x2", yaxis="y3"))

    _, record, _ = resampled(fig)

    assert record.axes == {0: "x2"} and record.y_axes == {0: "y3"}


# --- views: the sample of the range the browser shows ----------------------------------


def view(record, axes: dict[str, Any]) -> dict[str, Any]:
    """The answer to a view report of ``axes``, which must name a resampled trace's axis."""
    traces = view_update(record, {"axes": axes})
    assert traces is not None
    return traces


def test_a_view_is_sampled_from_the_range_in_view_reaching_one_point_past_each_edge():
    _, record, _ = resampled(go.Figure(line(10_000)))

    traces = view(record, {"x": [2000.5, 2100.5]})

    kept = traces["0"]["index_map"]
    assert kept[0] == 2000 and kept[-1] == 2101
    assert 20 < len(kept) <= BUDGET, "sampled to the budget, not to the overview's detail"
    assert list(traces["0"]["attributes"]["x"]) == kept


def test_a_view_small_enough_is_drawn_in_full_detail():
    _, record, _ = resampled(go.Figure(line(10_000)))

    traces = view(record, {"x": [100, 120]})

    assert traces["0"]["index_map"] == list(range(99, 122))


def test_a_range_arriving_as_a_tuple_is_the_same_view():
    """Shiny hands the browser's JSON arrays to the server as tuples."""
    _, record, _ = resampled(go.Figure(line(10_000)))

    traces = view(record, {"x": (100, 120)})

    assert traces["0"]["index_map"] == list(range(99, 122))


def test_a_reversed_axis_reports_its_range_high_to_low():
    _, record, _ = resampled(go.Figure(line(10_000)))

    forward = view(record, {"x": [100, 120]})
    backward = view(record, {"x": [120, 100]})

    assert forward["0"]["index_map"] == backward["0"]["index_map"] == list(range(99, 122))


def test_a_view_at_the_ends_stays_within_the_data():
    _, record, _ = resampled(go.Figure(line(10_000)))

    assert view(record, {"x": [-50, 3]})["0"]["index_map"] == [0, 1, 2, 3, 4]
    tail = view(record, {"x": [9997, 20_000]})["0"]["index_map"]
    assert tail == [9996, 9997, 9998, 9999]


@pytest.mark.parametrize("span", [None, [0], "wide", [0, float("inf")], [None, 5], [0, "a"]])
def test_an_autoranged_or_unreadable_view_is_the_overview(span):
    _, record, _ = resampled(go.Figure(line(10_000)))

    traces = view(record, {"x": span})

    assert traces["0"]["index_map"] == record.overview[0].tolist()


def test_only_traces_on_a_reported_axis_are_resampled():
    fig = go.Figure([line(10_000), line(10_000, xaxis="x2")])
    _, record, _ = resampled(fig)

    assert list(view(record, {"x2": [10, 20]})) == ["1"]
    assert view_update(record, {"axes": {"x3": [10, 20]}}) is None
    assert view_update(record, {"no": "axes"}) is None
    assert view_update(record, "junk") is None


def test_a_view_remaps_the_selection_into_its_sample():
    _, record, _ = resampled(go.Figure(line(10_000, selectedpoints=[105, 5000])))

    traces = view(record, {"x": [100, 120]})

    kept = traces["0"]["index_map"]
    assert [kept[i] for i in traces["0"]["attributes"]["selectedpoints"]] == [105]


def test_as_array_reads_plotlys_binary_arrays_in_any_shape():
    grid = np.arange(6, dtype=np.int16).reshape(2, 3)
    encoded = {"dtype": "i2", "bdata": base64.b64encode(grid.tobytes()).decode(), "shape": "2, 3"}
    as_list_shape = {**encoded, "shape": [2, 3]}

    assert np.array_equal(as_array(encoded), grid)
    assert np.array_equal(as_array(as_list_shape), grid)
