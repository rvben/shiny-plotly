"""extend_traces, restyle and relayout: in-place updates sent to a drawn figure."""

import asyncio
import json
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import plotly.graph_objects as go
import pytest
from shiny import App, Inputs, Outputs, Session, module, reactive, ui
from starlette.testclient import TestClient

from shiny_plotly import extend_traces, output_plotly, relayout, render_plotly, restyle

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    """
    The coroutine on its own loop in a worker thread. The main thread cannot run one
    while the browser tests' session-wide playwright fixture is alive (its sync API
    keeps a loop running there), and nothing awaited here needs that loop anyway.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def bar() -> go.Figure:
    return go.Figure(go.Bar(x=["a", "b"], y=[1, 2]))


@module.server
def mod_server(input: Inputs, output: Outputs, session: Session):
    @render_plotly
    def fig():
        return bar()

    @reactive.effect
    @reactive.event(input.go)
    async def _():
        await relayout("fig", {"title.text": "from the module"})


def make_app() -> App:
    np = pytest.importorskip("numpy")
    app_ui = ui.page_fluid(
        ui.input_action_button("go", "go"),
        output_plotly("fig"),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return bar()

        @reactive.effect
        @reactive.event(input.go)
        async def _():
            await extend_traces("fig", {"y": [np.array([3.5])]}, max_points=10)
            await extend_traces("fig", {"x": [["c"]], "y": [[4]]}, indices=0)
            await restyle("fig", {"marker.color": "red"})
            await restyle("fig", {"opacity": [0.5, 1]}, indices=[0, 1])
            await relayout("fig", {"title.text": "updated"})

        mod_server("m")

    return App(app_ui, server)


def custom_messages(client: TestClient) -> list[dict]:
    """Press the button like the browser would and collect the shiny-plotly messages."""
    with client.websocket_connect("/websocket/") as ws:
        ws.receive_json()
        ws.send_json({"method": "init", "data": {}})
        ws.send_json({"method": "update", "data": {"go": 1, "m-go": 1}})
        found: list[dict] = []
        for _ in range(50):
            msg = ws.receive_json()
            if "custom" in msg and "shiny-plotly" in msg["custom"]:
                found.append(msg["custom"]["shiny-plotly"])
            if len(found) == 6:
                return found
        pytest.fail(f"expected 6 shiny-plotly messages, got {found}")


@pytest.fixture(scope="module")
def messages() -> list[dict]:
    with TestClient(make_app()) as client:
        return custom_messages(client)


def test_extend_traces_sends_plotly_serialised_data_indices_and_max_points(messages):
    first, second = messages[0], messages[1]

    assert first["id"] == "fig"
    assert first["method"] == "extendTraces"
    assert json.loads(first["args"]) == [{"y": [[3.5]]}, None, 10]
    assert json.loads(second["args"]) == [{"x": [["c"]], "y": [[4]]}, [0], None]


def test_restyle_sends_the_update_and_optional_indices(messages):
    first, second = messages[2], messages[3]

    assert first["method"] == "restyle"
    assert json.loads(first["args"]) == [{"marker.color": "red"}, None]
    assert json.loads(second["args"]) == [{"opacity": [0.5, 1]}, [0, 1]]


def test_relayout_sends_the_layout_update(messages):
    msg = messages[4]

    assert msg["method"] == "relayout"
    assert json.loads(msg["args"]) == [{"title.text": "updated"}]


def test_ids_are_namespaced_inside_a_module(messages):
    msg = messages[5]

    assert msg["id"] == "m-fig"
    assert json.loads(msg["args"]) == [{"title.text": "from the module"}]


@pytest.mark.parametrize("bad", [0, -5, 2.5, "100", True])
def test_max_points_must_be_a_positive_integer(bad):
    """A bool too: plotly.js reads a JSON true as non-numeric and quietly drops the cap."""
    with pytest.raises(ValueError, match="max_points"):
        run(extend_traces("fig", {"y": [[1]]}, max_points=bad))


def test_outside_a_session_it_fails_loudly():
    with pytest.raises(RuntimeError):
        run(relayout("fig", {"title.text": "x"}))
