"""Snapshot payload fidelity and isolation from caller-owned mutable values."""

import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytest
from plotly.io.json import to_json_plotly

from shiny_plotly._html import as_fig_dict
from shiny_plotly._snapshot import snapshot


@pytest.mark.parametrize(
    "dates",
    [
        [datetime.date(2026, 1, 1)],
        [datetime.datetime(2026, 1, 1)],
        [datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)],
        [datetime.datetime(2026, 1, 1, tzinfo=ZoneInfo("Europe/Brussels"))],
        [np.datetime64("2026-01-01")],
        [pd.Timestamp("2026-01-01", tz="Europe/Brussels")],
        [],
    ],
)
def test_date_array_snapshots_own_the_buffer_and_match_plotlys_serializer(dates):
    fig = go.Figure(go.Scatter(x=np.array(dates, dtype=object), y=np.arange(len(dates))))
    trace = cast(Any, fig).data[0]
    before = to_json_plotly(trace.to_plotly_json())
    sent = as_fig_dict(fig, preserve_arrays=True)
    x = sent["data"][0]["x"]
    assert to_json_plotly(sent["data"][0]) == before
    assert not np.shares_memory(x, trace.x)
    if dates:
        if type(x[0]) is pd.Timestamp:
            assert x[0] is not cast(Any, trace).x[0]
        else:
            assert x[0] is cast(Any, trace).x[0], "immutable scalars need no reconstruction"
        cast(Any, trace).x.flags.writeable = True
        cast(Any, trace).x[0] = datetime.date(2000, 1, 1)
        assert to_json_plotly(sent["data"][0]) == before


def test_nested_customdata_frames_and_numeric_arrays_are_independently_owned():
    custom = np.empty(2, dtype=object)
    custom[:] = [{"values": [1]}, {"values": [2]}]
    fig = go.Figure(
        go.Scatter(x=np.arange(2), y=np.arange(2), customdata=custom),
        layout={"meta": {"nested": [3]}},
        frames=[go.Frame(data=[go.Scatter(y=np.arange(2))])],
    )
    sent = as_fig_dict(fig, preserve_arrays=True)
    assert to_json_plotly(sent) == to_json_plotly(
        {
            "data": [cast(Any, fig).data[0].to_plotly_json()],
            "layout": fig.layout.to_plotly_json(),
            "frames": [fig.frames[0].to_plotly_json()],
        }
    )
    cast(Any, fig).data[0].customdata[0]["values"].append(9)
    cast(Any, fig).data[0].y.flags.writeable = True
    cast(Any, fig).data[0].y[0] = 99
    fig.layout.meta["nested"].append(9)
    fig.frames[0].data[0].y.flags.writeable = True
    fig.frames[0].data[0].y[0] = 99
    assert sent["data"][0]["customdata"][0]["values"] == [1]
    assert sent["data"][0]["y"][0] == 0
    assert sent["layout"]["meta"]["nested"] == [3]
    assert sent["frames"][0]["data"][0]["y"][0] == 0


class MutableZone(datetime.tzinfo):
    def __init__(self):
        self.offset = datetime.timedelta(hours=2)

    def utcoffset(self, dt):
        return self.offset

    def dst(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "mutable"


def test_mutable_timezone_and_date_subclasses_keep_deepcopy_isolation():
    zone = MutableZone()
    x = np.array([datetime.datetime(2026, 1, 1, tzinfo=zone)], dtype=object)
    sent = snapshot(go.Scatter(x=x))
    original = go.Scatter(x=x)
    clone = snapshot(original)
    cast(Any, original).x[0].tzinfo.offset = datetime.timedelta(hours=3)
    assert clone["x"][0].utcoffset() == datetime.timedelta(hours=2)
    assert sent["x"][0].tzinfo is not zone

    class Date(datetime.date):
        extra: list[int]

        def __deepcopy__(self, memo):
            result = Date(self.year, self.month, self.day)
            result.extra = list(self.extra)
            return result

    day = Date(2026, 1, 1)
    day.extra = [1]
    original = go.Scatter(x=np.array([day], dtype=object))
    clone = snapshot(original)
    cast(Any, original).x[0].extra.append(2)
    assert clone["x"][0].extra == [1]


def test_snapshot_without_numpy_or_pandas_and_with_repeated_containers():
    trace = go.Scatter(x=np.array([datetime.date(2026, 1, 1)], dtype=object))
    with patch("shiny_plotly._snapshot.sys", SimpleNamespace(modules={"numpy": np})):
        assert snapshot(trace)["x"][0] is cast(Any, trace).x[0]
    with patch("shiny_plotly._snapshot.sys", SimpleNamespace(modules={})):
        assert snapshot(trace)["x"][0] == cast(Any, trace).x[0]

    # Metadata can share containers; traversing them must terminate and preserve aliases.
    repeated = {"nested": [1]}
    trace = go.Scatter(meta=[repeated, repeated])
    sent = snapshot(trace)
    assert sent["meta"][0] is sent["meta"][1]
    assert sent["meta"][0] is not cast(Any, trace).meta[0]


def test_custom_serializers_and_unknown_storage_keep_the_public_fallback():
    class Custom(go.Scatter):
        def to_plotly_json(self):
            return {"type": "scatter", "meta": "custom"}

    assert snapshot(Custom()) == {"type": "scatter", "meta": "custom"}
    empty = go.Scatter()
    with patch.object(type(empty), "_props", new_callable=lambda: property(lambda self: None)):
        assert snapshot(empty) == {}


def test_trace_snapshots_do_not_introduce_aliases_across_components():
    shared = {"dates": np.array([datetime.date(2026, 1, 1)], dtype=object), "values": [1]}
    fig = go.Figure([go.Scatter(meta=shared), go.Scatter(meta=shared)])
    # Plotly stores metadata as supplied, so it can share containers across traces.
    sent = as_fig_dict(fig, preserve_arrays=True)
    sent["data"][0]["meta"]["values"].append(2)
    assert sent["data"][1]["meta"]["values"] == [1]
    sent["data"][0]["meta"]["dates"][0] = datetime.date(2000, 1, 1)
    assert sent["data"][1]["meta"]["dates"][0] == datetime.date(2026, 1, 1)


def test_plain_date_lists_and_pytz_dates_keep_owned_buffers():
    dates = [datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 2)]
    trace = go.Scatter(x=dates)
    sent = snapshot(trace)
    assert sent["x"][0] is cast(Any, trace).x[0]
    assert sent["x"] is not cast(Any, trace).x

    pytz = pytest.importorskip("pytz")
    index = pd.date_range(
        "2021-10-31 01:00", periods=6, freq="h", tz=pytz.timezone("Europe/Brussels")
    )
    trace = go.Scatter(x=index.to_numpy())
    sent = snapshot(trace)
    assert sent["x"][0] is not cast(Any, trace).x[0]
    assert not np.shares_memory(sent["x"], cast(Any, trace).x)


def test_timestamps_with_caller_added_attributes_keep_the_original_copy_semantics():
    timestamp = pd.Timestamp("2026-01-01")
    cast(Any, timestamp).extra = [1]
    trace = go.Scatter(x=np.array([timestamp], dtype=object))
    sent = snapshot(trace)
    assert sent["x"][0] is not cast(Any, trace).x[0]
    assert to_json_plotly(sent) == to_json_plotly(trace.to_plotly_json())


def test_numeric_and_mixed_lists_keep_the_public_serializer_behavior():
    for values in ([1, 2, 3], [1, {"nested": [2]}], ({"nested": [1]}, 2)):
        trace = go.Scatter(meta=values)
        sent = snapshot(trace)
        assert to_json_plotly(sent) == to_json_plotly(trace.to_plotly_json())
    trace = go.Scatter(meta={"plain": [datetime.date(2026, 1, 1)]})
    with patch("shiny_plotly._snapshot.sys", SimpleNamespace(modules={})):
        assert snapshot(trace)["meta"]["plain"][0] is cast(Any, trace).meta["plain"][0]


def test_timestamp_attributes_added_after_snapshot_cannot_change_later_payloads():
    for x in (np.array([pd.Timestamp("2026-01-01")], dtype=object), [pd.Timestamp("2026-01-01")]):
        trace = go.Scatter(x=x)
        sent = snapshot(trace)
        before = to_json_plotly(sent)
        cast(Any, trace).x[0].isoformat = lambda: "mutated"
        assert to_json_plotly(sent) == before


def test_figure_serializer_overrides_are_respected_and_the_result_is_owned():
    raw = {
        "data": [{"type": "scatter", "x": np.arange(2), "y": np.arange(2)}],
        "layout": {"title": {"text": "custom"}},
    }

    class Custom(go.Figure):
        def to_dict(self):
            return raw

    sent = as_fig_dict(Custom(), preserve_arrays=True)
    assert sent["layout"]["title"]["text"] == "custom"
    raw["data"][0]["y"][0] = 99
    assert sent["data"][0]["y"][0] == 0


def test_object_date_arrays_preserve_fortran_order_and_own_timestamp_instances():
    dates = np.asfortranarray(np.array([[pd.Timestamp("2026-01-01")] * 2] * 2, dtype=object))
    trace = go.Scatter(meta={"dates": dates})
    sent = snapshot(trace)["meta"]["dates"]
    assert sent.flags.f_contiguous
    np.testing.assert_array_equal(sent, dates)
    assert not np.shares_memory(sent, dates)


def test_mixed_timestamp_zones_and_submicrosecond_precision_match_deepcopy():
    dates = np.array(
        [
            pd.Timestamp("2026-01-01", tz="UTC"),
            pd.Timestamp("2026-01-02", tz="Europe/Brussels"),
        ],
        dtype=object,
    )
    trace = go.Scatter(x=dates)
    assert to_json_plotly(snapshot(trace)) == to_json_plotly(trace.to_plotly_json())
    dates = np.array([pd.Timestamp("1969-12-31T23:59:59.999999999")], dtype=object)
    trace = go.Scatter(x=dates)
    assert snapshot(trace)["x"][0].value == cast(Any, trace).x[0].value
    dates = [datetime.date(2026, 1, 1), pd.Timestamp("2026-01-02")]
    trace = go.Scatter(x=dates)
    sent = snapshot(trace)
    assert to_json_plotly(sent) == to_json_plotly(trace.to_plotly_json())
    assert sent["x"][1] is not cast(Any, trace).x[1]


def test_timestamp_clones_preserve_pytz_fold_offsets_and_nanoseconds():
    pytz = pytest.importorskip("pytz")
    values = list(
        pd.date_range("2021-10-31 01:00", periods=6, freq="h", tz=pytz.timezone("Europe/Brussels"))
    )
    values.append(values[-1] + pd.Timedelta(1, unit="ns"))
    trace = go.Scatter(x=np.array(values, dtype=object))
    sent = snapshot(trace)
    source = cast(Any, trace).x
    assert list(sent["x"]) == list(source)
    assert [v.utcoffset() for v in sent["x"]] == [v.utcoffset() for v in source]
    assert [v.nanosecond for v in sent["x"]] == [v.nanosecond for v in source]


def test_large_timestamp_years_and_failed_scalar_clones_retain_deepcopy(monkeypatch):
    huge = pd.Timestamp(np.datetime64("20000-01-01", "s")).tz_localize("UTC")
    for dates in ([huge], [datetime.date(2026, 1, 1), huge]):
        trace = go.Scatter(x=np.array(dates, dtype=object))
        sent = snapshot(trace)
        assert sent["x"][-1].asm8 == cast(Any, trace).x[-1].asm8

    # An unsupported public replacement must not turn a formerly valid snapshot
    # into a failure. Force this version-dependent fallback with an ordinary date.
    def unsupported(*args, **kwargs):
        raise ValueError("not representable")

    monkeypatch.setattr(pd.Timestamp, "replace", unsupported)
    trace = go.Scatter(
        x=np.array([datetime.date(2026, 1, 1), pd.Timestamp("2026-01-02")], dtype=object)
    )
    assert snapshot(trace)["x"][1] == cast(Any, trace).x[1]


def test_bulk_and_mixed_gap_timestamp_snapshots_match_plotlys_deepcopy_payload():
    from zoneinfo import ZoneInfo

    gap = pd.Timestamp(datetime.datetime(2021, 3, 28, 2, 30, tzinfo=ZoneInfo("Europe/Brussels")))
    for dates in ([gap], [datetime.date(2021, 3, 27), gap]):
        trace = go.Scatter(x=np.array(dates, dtype=object))
        assert to_json_plotly(snapshot(trace)) == to_json_plotly(trace.to_plotly_json())
