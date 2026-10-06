"""The guard that keeps in-place updates from undoing what a resampled output relies on."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest

from shiny_plotly._views import (
    attribute_paths,
    check_update,
    output_view,
    per_point_attributes,
    sampled_output,
)


class Sampled:
    """Traces 0 and 2 of four resampled: 0 on x/y with a colour per point, 2 on x2/y2."""

    trace_count = 4
    axes: ClassVar = {0: "x", 2: "x2"}
    y_axes: ClassVar = {0: "y", 2: "y2"}
    per_point: ClassVar = {0: frozenset({"x", "y", "marker.color"}), 2: frozenset({"x", "y"})}


class FakeSession:
    """What the guard reads of a session: its root scope, and a place to be told it ended."""

    def __init__(self) -> None:
        self.ended: list[Any] = []

    def root_scope(self) -> FakeSession:
        return self

    def on_ended(self, callback: Any) -> None:
        self.ended.append(callback)


@pytest.fixture
def session() -> Any:
    session: Any = FakeSession()
    output_view(session, "fig").sampled = cast(Any, Sampled())
    return session


def refused(session: Any, method: str, *args: Any, name: str = "fig") -> str | None:
    try:
        check_update(session, name, method, args)
    except ValueError as err:
        return str(err)
    return None


@pytest.mark.parametrize("method", ["extendTraces", "prependTraces", "addTraces", "deleteTraces"])
def test_adding_removing_or_shifting_points_or_traces_is_refused(session, method):
    message = refused(session, method, {}, None)

    assert message is not None
    assert message.startswith(f"output 'fig' is rendered with resample=, so {method}")
    assert message.endswith("re-render the output instead")


@pytest.mark.parametrize(
    "update",
    [
        {"y": [[1, 2]]},
        {"x": [[1, 2]]},
        {"y": 3},
        {"customdata": [[1]]},
        {"text": [["a", "b"]]},
        {"hovertext": [["a"]]},
        {"marker.size": [[3, 4]]},
        {"marker": {"size": [3, 4]}},
        {"selectedpoints": [[1]]},
        {"selectedpoints": None},
        {"connectgaps": True},
        {"xperiod": 2},
        {"xcalendar": "coptic"},
        {"error_y.array": [[1]]},
        {"error_y": {"visible": True}},
    ],
)
def test_restyling_what_a_sample_depends_on_is_refused_on_a_resampled_trace(session, update):
    for indices in (None, [0], [2], [-2], [1, 2]):
        assert refused(session, "restyle", update, indices) is not None, indices


@pytest.mark.parametrize("update", [{"y": [[1, 2]]}, {"marker.size": [[1, 2]]}, {"text": "a"}])
def test_the_same_restyle_passes_on_a_trace_that_is_not_resampled(session, update):
    assert refused(session, "restyle", update, [1, 3]) is None
    assert refused(session, "restyle", update, [-1]) is None


@pytest.mark.parametrize(
    "update",
    [
        {"marker.color": "red"},
        {"marker": {"color": "red"}},
        {"marker": None},
        {"marker.color[3]": "red"},
    ],
)
def test_a_per_point_attribute_the_trace_carries_is_refused_even_as_one_value(session, update):
    """The next view's sample would restyle the full data's colours back over it."""
    assert refused(session, "restyle", update, [0]) is not None
    assert refused(session, "restyle", update, [2]) is None, "trace 2 has no colour per point"


@pytest.mark.parametrize(
    "update",
    [
        {"name": "renamed"},
        {"opacity": 0.5},
        {"line.color": "red"},
        {"line": {"width": 3}},
        {"visible": "legendonly"},
        {"hoverinfo": "x"},
        {"text": "one label for every point"},
        {"marker.size": 6},
        {"marker": {"line": {"width": 2}}},
        {"marker.colorbar.title.text": "scale"},
        {"meta": [1, 2]},
    ],
)
def test_restyling_the_look_of_a_resampled_trace_passes(session, update):
    assert refused(session, "restyle", update, None) is None


def test_a_top_level_list_is_one_value_per_trace_in_turn(session):
    """["a", "b"] labels trace after trace; only a list for a resampled trace is refused."""
    assert refused(session, "restyle", {"text": ["a", "b"]}, [0, 2]) is None
    assert refused(session, "restyle", {"text": [["a", "b"], "c"]}, [1, 2]) is None
    assert refused(session, "restyle", {"text": [["a", "b"], "c"]}, [2, 1]) is not None
    assert refused(session, "restyle", {"text": [["a"], "c"]}, None) is not None, "cycles to 2"


@pytest.mark.parametrize("key", ["type", "stackgroup", "fill", "xaxis", "yaxis"])
def test_linking_attributes_are_refused_on_every_trace(session, key):
    """A trace that is not resampled can still become a fill or stack partner of one."""
    message = refused(session, "restyle", {key: "x"}, [1])

    assert message is not None and f"restyling {key!r}" in message


def test_update_is_checked_for_its_restyle_and_its_indices(session):
    assert refused(session, "update", {"y": [[1]]}, {}, [0]) is not None
    assert refused(session, "update", {"y": [[1]]}, {}, [1]) is None
    assert refused(session, "update", {"y": [[1]]}, {}) is not None, "no indices: all"
    assert refused(session, "update", {"fill": "tonexty"}, {}, [3]) is not None


@pytest.mark.parametrize(
    "layout",
    [
        {"xaxis.type": "category"},
        {"xaxis": {"type": "log"}},
        {"xaxis.rangeslider.visible": True},
        {"xaxis.rangeslider": {"visible": True}},
        {"xaxis": {"rangeslider": {}}},
        {"xaxis": None},
        {"xaxis2.type": "date"},
        {"yaxis.type": "log"},
        {"yaxis2": {"type": "linear"}},
    ],
)
def test_relayout_of_what_decides_a_resampled_axis_is_refused(session, layout):
    message = refused(session, "relayout", layout)

    assert message is not None and "relayout of" in message
    assert refused(session, "update", {}, layout) is not None


@pytest.mark.parametrize(
    "layout",
    [
        {"xaxis.range": [0, 1]},
        {"xaxis.autorange": True},
        {"xaxis": {"title": {"text": "t"}}},
        {"yaxis.range": [0, 1]},
        {"yaxis.rangeslider.visible": True},
        {"xaxis3.type": "category"},
        {"yaxis3.type": "log"},
        {"title.text": "new"},
    ],
)
def test_other_relayouts_pass(session, layout):
    assert refused(session, "relayout", layout) is None


def test_outputs_that_are_not_resampled_take_every_update():
    session: Any = FakeSession()
    output_view(session, "plain")  # rendered with resample=, but nothing was long enough

    assert refused(session, "extendTraces", {}, None, name="plain") is None
    assert refused(session, "extendTraces", {}, None, name="never_seen") is None
    assert refused(FakeSession(), "restyle", {"y": [[1]]}, None) is None


def test_the_state_goes_when_the_session_ends(session):
    assert sampled_output(session, "fig") is not None
    assert len(session.ended) == 1, "registered once, however many outputs"
    output_view(session, "other")
    assert len(session.ended) == 1

    session.ended[0]()

    assert sampled_output(session, "fig") is None


def test_attribute_paths_names_each_leaf_a_restyle_sets():
    update = {"marker": {"color": 1, "line": {"width": 2}}, "y": [1], "line.dash[0]": "dot"}

    paths = ["line.dash", "marker.color", "marker.line.width", "y"]
    assert sorted(attribute_paths(update)) == paths
    assert attribute_paths({"marker": {}}) == ["marker"]


def test_per_point_attributes_come_from_plotlys_own_validators():
    attributes = per_point_attributes()

    for path in (
        "x",
        "y",
        "text",
        "hovertext",
        "customdata",
        "ids",
        "marker.color",
        "marker.size",
        "marker.line.color",
        "error_y.array",
        "hovertemplate",
    ):
        assert path in attributes, path
    for path in (
        "name",
        "line.color",
        "meta",
        "marker.colorbar.tickvals",
        "fillpattern.shape",
        "opacity",
        "mode",
    ):
        assert path not in attributes, path


def test_excluded_colorbar_properties_do_not_need_validators(monkeypatch):
    from plotly.basedatatypes import BasePlotlyType

    original = BasePlotlyType._get_validator

    def get_validator(self, prop):
        if self._path_str in ("scatter.marker.colorbar", "scattergl.marker.colorbar"):
            raise AssertionError("excluded colorbar validators must not be inspected")
        return original(self, prop)

    monkeypatch.setattr(BasePlotlyType, "_get_validator", get_validator)
    per_point_attributes.cache_clear()
    try:
        attributes = per_point_attributes()
        assert "marker.color" in attributes
        assert "marker.colorbar.tickvals" not in attributes
    finally:
        per_point_attributes.cache_clear()
