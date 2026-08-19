import json

import plotly
import plotly.graph_objects as go
import pytest
from shiny import App, Inputs, Outputs, Session, module, ui
from shiny.render.renderer import Renderer
from starlette.testclient import TestClient

from shiny_plotly import __version__, output_plotly, plotly_js, render_plotly


def bar() -> go.Figure:
    return go.Figure(go.Bar(x=["a", "b"], y=[1, 2]))


# --- decorator shape ------------------------------------------------------------------


def test_bare_decorator_keeps_the_function_name_as_output_id():
    @render_plotly
    def sales():
        return bar()

    assert isinstance(sales, Renderer)
    assert sales.output_id == "sales"


def test_parenthesised_decorator_keeps_the_output_id_and_records_options():
    @render_plotly(height="300px", figurewidget_margins=True, config={"displaylogo": False})
    def sales():
        return bar()

    assert sales.output_id == "sales"
    assert sales.height == "300px"
    assert sales.figurewidget_margins is True
    assert sales.config == {"displaylogo": False}


def test_express_auto_output_ui_is_the_plotly_output_with_the_output_id():
    @render_plotly
    def sales():
        return bar()

    tag = sales.auto_output_ui()

    assert tag.attrs["id"] == "sales"
    assert tag.has_class("shiny-plotly-output")
    assert tag.has_class("html-fill-item")
    assert tag.has_class("html-fill-container")


def test_output_plotly_is_a_fill_aware_output_bound_by_the_browser_helper():
    tag = output_plotly("sales")

    assert tag.attrs["id"] == "sales"
    assert tag.has_class("shiny-plotly-output"), "the class the output binding finds"
    assert not tag.has_class("shiny-html-output"), "not an output_ui: values are figures"
    assert tag.has_class("html-fill-item")
    assert tag.has_class("html-fill-container")


def test_output_plotly_carries_the_bundle_and_the_helper_so_no_page_level_call_is_needed():
    deps = output_plotly("sales").get_dependencies()

    assert [(d.name, str(d.version)) for d in deps] == [
        ("plotly", plotly.__version__),
        ("shiny-plotly", __version__),
    ]


def test_output_plotly_is_namespaced_inside_a_module():
    @module.ui
    def mod_ui():
        return output_plotly("fig")

    assert mod_ui("m").attrs["id"] == "m-fig"


def test_express_auto_output_ui_is_namespaced_inside_a_module():
    @render_plotly
    def fig():
        return bar()

    @module.ui
    def mod_ui():
        return fig.auto_output_ui()

    assert mod_ui("m").attrs["id"] == "m-fig"


def test_output_plotly_with_a_height_is_a_fixed_size_fill_container():
    tag = output_plotly("sales", height="220px", width="400px")

    assert tag.has_class("html-fill-container"), "the graph inside still fills the output"
    assert not tag.has_class("html-fill-item"), "a fill item would not keep its 220px"
    assert "height:220px" in str(tag.attrs["style"]).replace(" ", "")
    assert "width:400px" in str(tag.attrs["style"]).replace(" ", "")


# --- end to end through a real Shiny app ----------------------------------------------


def make_app() -> App:
    app_ui = ui.page_fluid(
        plotly_js(),
        output_plotly("sync_fig"),
        output_plotly("async_fig"),
        output_plotly("empty_fig"),
        output_plotly("parity_fig"),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def sync_fig():
            return bar()

        @render_plotly
        async def async_fig():
            return bar().update_layout(title="async")

        @render_plotly
        def empty_fig():
            return None

        @render_plotly(
            figurewidget_margins=True,
            height="250px",
            width="300px",
            config={"displaylogo": False},
            post_script="console.log('{plot_id}')",
        )
        def parity_fig():
            return bar()

    return App(app_ui, server)


OUTPUT_IDS = ("sync_fig", "async_fig", "empty_fig", "parity_fig")


def first_flush(client: TestClient) -> dict:
    """Connect like the browser does and return the first message carrying output values."""
    with client.websocket_connect("/websocket/") as ws:
        assert "config" in ws.receive_json()
        ws.send_json(
            {
                "method": "init",
                "data": {f".clientdata_output_{oid}_hidden": False for oid in OUTPUT_IDS},
            }
        )
        for _ in range(50):
            msg = ws.receive_json()
            if "values" in msg and all(oid in msg["values"] for oid in OUTPUT_IDS):
                assert msg.get("errors") in (None, {}), msg.get("errors")
                return msg["values"]
        pytest.fail("no output values flushed after init")


@pytest.fixture(scope="module")
def values() -> dict:
    with TestClient(make_app()) as client:
        return first_flush(client)


def test_sync_figure_is_sent_as_figure_json_for_the_browser_helper_to_draw(values):
    value = values["sync_fig"]

    figure = json.loads(value["figure"])
    assert figure["data"][0]["type"] == "bar"
    assert figure["data"][0]["x"] == ["a", "b"]
    assert "layout" in figure
    assert value["config"] == {"responsive": True}
    assert value["height"] is None
    assert value["width"] == "100%"
    assert value["post_script"] is None


def test_figure_json_is_plotly_serialised_not_shiny_serialised():
    """numpy arrays only survive plotly's encoder, which also packs them as compact bdata."""
    np = pytest.importorskip("numpy")

    app_ui = ui.page_fluid(output_plotly("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return go.Figure(go.Scatter(x=np.arange(3), y=np.array([1.5, 2.5, 3.5])))

    with TestClient(App(app_ui, server)) as client:
        value = flush_one(client, "fig")

    trace = json.loads(value["figure"])["data"][0]
    assert trace["x"] == {"dtype": "i1", "bdata": "AAEC"}
    assert trace["y"] == {"dtype": "f8", "bdata": "AAAAAAAA+D8AAAAAAAAEQAAAAAAAAAxA"}


def test_async_render_function_is_awaited(values):
    figure = json.loads(values["async_fig"]["figure"])

    assert figure["layout"]["title"]["text"] == "async"


def test_none_renders_as_an_empty_output(values):
    assert values["empty_fig"] is None


def test_renderer_options_reach_the_value(values):
    value = values["parity_fig"]

    assert json.loads(value["figure"])["layout"]["margin"] == {"l": 16, "t": 32, "r": 16, "b": 16}
    assert value["height"] == "250px"
    assert value["width"] == "300px"
    assert value["config"] == {"responsive": True, "displaylogo": False}
    assert value["post_script"] == "console.log('{plot_id}')"


def flush_one(client: TestClient, output_id: str) -> dict:
    with client.websocket_connect("/websocket/") as ws:
        ws.receive_json()
        ws.send_json({"method": "init", "data": {f".clientdata_output_{output_id}_hidden": False}})
        for _ in range(50):
            msg = ws.receive_json()
            if "values" in msg and output_id in msg["values"]:
                return msg["values"][output_id]
        pytest.fail("no output values flushed after init")


def test_plotly_bundle_is_served_from_the_page_level_dependency():
    with TestClient(make_app()) as client:
        resp = client.get(f"/lib/plotly-{plotly.__version__}/plotly.min.js")

    assert resp.status_code == 200
    assert b"Plotly" in resp.content[:200_000]
    assert len(resp.content) > 1_000_000


def test_bundle_and_helper_are_served_from_the_output_tag_without_a_page_level_call():
    app_ui = ui.page_fluid(output_plotly("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return bar()

    with TestClient(App(app_ui, server)) as client:
        bundle = client.get(f"/lib/plotly-{plotly.__version__}/plotly.min.js")
        helper = client.get(f"/lib/shiny-plotly-{__version__}/shiny-plotly.js")

    assert bundle.status_code == 200
    assert len(bundle.content) > 1_000_000
    assert helper.status_code == 200
    assert b"ResizeObserver" in helper.content
    assert b"Plotly.react(" in helper.content
