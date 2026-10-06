"""render_plotly(resample=...) end to end through a real Shiny session: the first render,
the answers to view reports, re-renders, modules, the update guard and the cleanup."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import numpy as np
import plotly.graph_objects as go
import plotly.io as pio
import pytest
from shiny import App, Inputs, Outputs, Session, module, reactive, req, ui
from shiny.types import SilentOperationInProgressException
from starlette.testclient import TestClient

from shiny_plotly import extend_traces, output_plotly, relayout, render_plotly, restyle
from shiny_plotly._html import as_fig_dict, encode_figure_arrays, plotly_utils
from shiny_plotly._resample import as_array, resample_figure
from shiny_plotly._views import OutputView, _outputs

from helpers import run

N = 5000
BUDGET = 100
VIEW = "__shiny_plotly_view"


def long_figure(offset: float = 0.0) -> go.Figure:
    x = np.arange(N)
    return go.Figure(
        [
            go.Scattergl(x=x, y=np.sin(x / 50.0) + offset, customdata=x),
            go.Scatter(x=[0, 1], y=[0, 1]),
        ]
    )


@module.server
def mod_server(input: Inputs, output: Outputs, session: Session):
    @render_plotly(resample=BUDGET)
    def fig():
        return long_figure()


def make_app(sessions: list[Session]) -> App:
    app_ui = ui.page_fluid(output_plotly("fig"), output_plotly("short"))

    def server(input: Inputs, output: Outputs, session: Session):
        sessions.append(session)

        @render_plotly(resample=BUDGET)
        def fig():
            mode = input.mode() if "mode" in input else "long"
            if mode == "empty":
                return None
            if mode == "cancel":
                req(False, cancel_output=True)
            if mode == "progress":
                raise SilentOperationInProgressException()
            if mode == "error":
                raise ValueError("intentional rendering failure")
            return long_figure(1.0 if mode == "again" else 0.0)

        @render_plotly(resample=BUDGET)
        def short():
            return go.Figure(go.Scatter(x=[1, 2], y=[3, 4]))

        @reactive.effect
        @reactive.event(input.ping)
        async def _():
            await session.send_custom_message("ping", {"n": input.ping()})

        @reactive.effect
        @reactive.event(input.try_updates)
        async def _():
            async def attempt(name, coro):
                try:
                    await coro
                    outcome = "sent"
                except ValueError as err:
                    outcome = str(err)
                await session.send_custom_message("guard", {"name": name, "outcome": outcome})

            await attempt("extend", extend_traces("fig", {"y": [[1]]}, 0))
            await attempt("restyle y", restyle("fig", {"y": [[1, 2]]}, 0))
            await attempt("restyle name", restyle("fig", {"name": "a"}, 0))
            await attempt("relayout type", relayout("fig", {"xaxis.type": "category"}))
            await attempt("extend short", extend_traces("short", {"y": [[1]]}, 0))

        mod_server("m")

    return App(app_ui, server)


class Client:
    """One browser-like session: the values it was sent and the custom messages."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.values: dict = {}

    def send(self, **data) -> None:
        self.ws.send_json({"method": "update", "data": data})

    def until(self, wanted) -> dict:
        """Read until ``wanted(msg)`` holds for a message; return that message."""
        for _ in range(100):
            msg = self.ws.receive_json()
            self.values.update(msg.get("values") or {})
            if wanted(msg):
                return msg
        pytest.fail("the message never came")

    def custom(self, kind: str) -> dict:
        return self.until(lambda m: kind in (m.get("custom") or {}))["custom"][kind]

    def value(self, output_id: str) -> dict:
        """The output's value, waiting for the first one."""
        if output_id not in self.values:
            self.until(lambda m: output_id in (m.get("values") or {}))
        return self.values[output_id]

    def report(self, output_id: str, revision, seq: int, axes: dict) -> None:
        self.send(**{output_id + VIEW: {"revision": revision, "seq": seq, "axes": axes}})

    def answer(self) -> dict:
        """The next resample message, as the browser would decode it."""
        message = self.custom("shiny-plotly")
        assert message["method"] == "resample"
        revision, seq, traces = json.loads(message["args"])
        return {"id": message["id"], "revision": revision, "seq": seq, "traces": traces}

    def nothing_before_ping(self, n: int) -> None:
        """No shiny-plotly message arrives before the ping that follows it."""
        self.send(ping=n)
        msg = self.until(lambda m: m.get("custom"))
        assert "ping" in msg["custom"], msg["custom"]


@pytest.fixture(scope="module")
def sessions() -> list[Session]:
    return []


@pytest.fixture(scope="module")
def app_client(sessions) -> Iterator[TestClient]:
    with TestClient(make_app(sessions)) as client:
        yield client


@contextmanager
def connect(client: TestClient) -> Iterator[Client]:
    outputs = ("fig", "short", "m-fig")
    with client.websocket_connect("/websocket/") as ws:
        ws.receive_json()
        hidden = {f".clientdata_output_{oid}_hidden": False for oid in outputs}
        ws.send_json({"method": "init", "data": hidden})
        yield Client(ws)


@pytest.fixture
def browser(app_client) -> Iterator[Client]:
    with connect(app_client) as client:
        yield client


def test_the_first_render_sends_the_overview_and_what_the_browser_needs_to_zoom(browser):
    value = browser.value("fig")
    figure = json.loads(value["figure"])

    assert value["resample"]["revision"] == 1
    assert value["resample"]["axes"] == {"0": "x"}
    index_map = value["resample"]["index_maps"]["0"]
    assert len(index_map) <= BUDGET and index_map[0] == 0 and index_map[-1] == N - 1
    assert len(figure["data"][0]["y"]) == len(index_map), "the sample, not 5000 points"
    assert figure["data"][1]["y"] == [0, 1], "a short trace travels whole"


def test_an_output_with_nothing_long_enough_draws_as_usual(browser):
    assert browser.value("short")["resample"] is None


def test_a_view_report_is_answered_with_a_sample_of_that_range(browser):
    browser.value("fig")
    browser.report("fig", 1, 1, {"x": [1000.5, 1030.5]})

    answer = browser.answer()

    assert answer["id"] == "fig" and answer["revision"] == 1 and answer["seq"] == 1
    trace = answer["traces"]["0"]
    assert trace["index_map"] == list(range(1000, 1032))
    assert trace["attributes"]["customdata"] == list(range(1000, 1032))
    assert list(answer["traces"]) == ["0"], "the short trace is left as drawn"


def test_a_reset_view_is_answered_with_the_overview(browser):
    overview = browser.value("fig")["resample"]["index_maps"]["0"]
    browser.report("fig", 1, 5, {"x": None})

    assert browser.answer()["traces"]["0"]["index_map"] == overview


def test_a_report_about_an_earlier_render_or_unknown_axes_is_not_answered(browser):
    browser.value("fig")
    browser.report("fig", 0, 1, {"x": [0, 10]})
    browser.report("fig", 1, 2, {"x7": [0, 10]})
    browser.send(**{"fig" + VIEW: "junk"})
    browser.nothing_before_ping(1)

    browser.report("fig", 1, 3, {"x": [0, 10]})
    assert browser.answer()["seq"] == 3


def test_a_re_render_moves_the_revision_on_and_answers_from_the_new_data(browser):
    browser.value("fig")
    browser.send(mode="again")
    browser.until(lambda m: "fig" in (m.get("values") or {}))
    assert browser.values["fig"]["resample"]["revision"] == 2

    browser.report("fig", 1, 1, {"x": [0, 10]})
    browser.nothing_before_ping(2)
    browser.report("fig", 2, 2, {"x": [0, 10]})
    answer = browser.answer()

    assert answer["revision"] == 2
    assert answer["traces"]["0"]["attributes"]["y"][0] == pytest.approx(1.0), "the new data"
    browser.nothing_before_ping(3)  # one watcher per output: one answer per report


def test_a_render_that_returns_nothing_lets_go_of_the_data(browser, sessions):
    browser.value("fig")
    browser.send(mode="empty")
    browser.until(lambda m: (m.get("values") or {}).get("fig", "") is None)

    browser.report("fig", 2, 1, {"x": [0, 10]})
    browser.nothing_before_ping(4)
    assert _outputs[sessions[-1]]["fig"].sampled is None


def test_an_output_in_a_module_is_reported_and_answered_under_its_namespaced_id(browser):
    browser.value("m-fig")
    browser.report("m-fig", 1, 1, {"x": [10, 20]})

    answer = browser.answer()

    assert answer["id"] == "m-fig"
    assert answer["traces"]["0"]["index_map"] == list(range(9, 22))


def test_in_place_updates_that_would_break_the_sample_are_refused(browser):
    browser.value("fig")
    browser.value("short")
    browser.send(try_updates=1)

    outcomes = {}
    while len(outcomes) < 5:
        message = browser.custom("guard")
        outcomes[message["name"]] = message["outcome"]

    assert outcomes["extend"].startswith("output 'fig' is rendered with resample=, so extend")
    assert "restyling 'y' of resampled trace 0" in outcomes["restyle y"]
    assert outcomes["restyle name"] == "sent"
    assert "relayout of 'xaxis.type'" in outcomes["relayout type"]
    assert outcomes["extend short"] == "sent", "nothing of it is resampled"


def test_each_session_has_its_own_data_and_lets_go_of_it_when_it_ends(app_client, sessions):
    with connect(app_client) as first:
        first.value("fig")
        with connect(app_client) as second:
            second.value("fig")
            one, two = sessions[-2], sessions[-1]
            assert _outputs[one]["fig"].sampled is not _outputs[two]["fig"].sampled
        second_gone = two not in _outputs
        first.nothing_before_ping(1)
    assert second_gone, "freed as its session ended, not whenever it is collected"


# --- the option itself -----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["cancel", "progress"])
def test_cancelled_render_keeps_zoom_answers_and_update_guards(app_client, sessions, mode):
    with connect(app_client) as client:
        revision = client.value("fig")["resample"]["revision"]
        state = _outputs[sessions[-1]]["fig"]
        previous = state.sampled
        client.send(mode=mode)
        client.nothing_before_ping(1)
        assert state.revision == revision and state.sampled is previous
        client.report("fig", revision, 1, {"x": [100, 150]})
        assert client.answer()["traces"]["0"]["index_map"] == list(range(99, 152))
        client.send(try_updates=1)
        assert "resample=" in client.custom("guard")["outcome"]


def test_visible_render_error_invalidates_the_previous_sample(app_client, sessions):
    with connect(app_client) as client:
        revision = client.value("fig")["resample"]["revision"]
        state = _outputs[sessions[-1]]["fig"]
        client.send(mode="error")
        client.until(lambda message: "fig" in (message.get("errors") or {}))
        assert state.revision > revision and state.sampled is None


def test_async_value_function_retains_the_displayed_sample_until_it_returns(monkeypatch):
    from shiny_plotly import _render

    _, previous = resample_figure(long_figure().to_dict(), BUDGET)
    state = OutputView(revision=5, sampled=previous)
    monkeypatch.setattr(_render, "output_view", lambda *args: state)

    @render_plotly(resample=BUDGET)
    async def fig():
        assert state.sampled is previous and state.revision == 5
        await asyncio.sleep(0)
        assert state.sampled is previous and state.revision == 5
        return None

    monkeypatch.setattr(fig, "_output_name", lambda: (None, "fig"))
    assert run(fig.render()) is None
    assert state.sampled is None and state.revision == 6


@pytest.mark.parametrize("bad", [0, 9, -5, True, 2.5, "100"])
def test_resample_must_be_a_whole_number_of_at_least_ten(bad):
    with pytest.raises((ValueError, TypeError)):
        render_plotly(resample=bad)


def test_resample_takes_a_numpy_integer_and_defaults_to_off():
    assert render_plotly(resample=cast(Any, np.int64(500))).resample == 500
    assert render_plotly().resample is None


def test_resample_without_numpy_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)

    with pytest.raises(ImportError, match=r"install shiny-plotly\[resample\]"):
        render_plotly(resample=100)


def test_outside_a_session_the_overview_is_sent_with_no_revision():
    @render_plotly(resample=BUDGET)
    def fig():
        return long_figure()

    value = cast(dict[str, Any], run(fig.render()))

    assert value["resample"]["revision"] is None
    assert len(value["resample"]["index_maps"]["0"]) <= BUDGET


@pytest.mark.parametrize("frames", [False, True])
def test_native_figure_conversion_preserves_properties_and_copies_arrays(frames):
    figure = long_figure()
    figure.update_layout(title="Original", xaxis={"range": [10, 20]})
    if frames:
        figure.frames = [go.Frame(name="next", data=[go.Scatter(y=np.arange(5.0))])]
    before = pio.to_json(figure)

    native = as_fig_dict(figure, preserve_arrays=True)

    assert isinstance(native["data"][0]["x"], np.ndarray)
    np.testing.assert_array_equal(native["data"][0]["x"], figure.data[0]["x"])
    encoded = as_fig_dict(figure, preserve_arrays=True)
    encode_figure_arrays(encoded)
    assert pio.to_json(encoded, validate=False) == before
    native["data"][0]["y"][0] = 999
    native["layout"]["xaxis"]["range"][0] = 99
    if frames:
        native["frames"][0]["data"][0]["y"][0] = 999
    assert pio.to_json(figure) == before, "sampling and layout edits cannot mutate the figure"


@pytest.mark.parametrize("from_dict", [False, True])
def test_resampling_skips_full_figure_encoding_without_changing_the_sample(monkeypatch, from_dict):
    figure = long_figure()
    trace = cast(Any, figure.data[0])
    trace.y = np.where(np.arange(N) % 101 == 0, np.nan, trace.y)
    trace.marker.color = np.arange(N, dtype=np.float32)
    trace.selectedpoints = [0, 30, 3000]
    # Include a whole, non-resampled trace to check its final encoding too.
    figure.add_trace(go.Heatmap(z=np.arange(12, dtype=np.float32).reshape(3, 4)))
    before = figure.to_dict()
    expected, record = resample_figure(before, BUDGET)
    expected_json = pio.to_json(expected, validate=False, remove_uids=False)

    def no_full_encoding():
        pytest.fail("resampling encoded the full figure before sampling")

    monkeypatch.setattr(figure, "to_dict", no_full_encoding)
    rendered = cast(
        dict[str, Any],
        run(render_plotly(resample=BUDGET).transform(before if from_dict else figure)),
    )

    assert rendered["figure"] == expected_json
    assert rendered["resample"]["index_maps"]["0"] == record.overview[0].tolist()
    np.testing.assert_array_equal(trace.y, as_array(before["data"][0]["y"]))


def test_native_arrays_still_serialize_without_plotlys_binary_encoder(monkeypatch):
    monkeypatch.delattr(plotly_utils, "convert_to_base64", raising=False)
    native = {"data": [{"type": "scatter", "y": np.arange(3.0)}], "layout": {}}

    encode_figure_arrays(native)

    assert json.loads(cast(str, pio.to_json(native, validate=False)))["data"][0]["y"] == [0, 1, 2]
