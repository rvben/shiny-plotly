import plotly
import plotly.graph_objects as go
import pytest
from shiny import App, Inputs, Outputs, Session, ui
from shiny.render.renderer import Renderer
from starlette.testclient import TestClient

from shiny_plotly import __version__, output_plotly, plotly_js, render_plotly

from newplot import parse_newplot


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


def test_express_auto_output_ui_is_a_fill_aware_output_ui_with_the_output_id():
    @render_plotly
    def sales():
        return bar()

    tag = sales.auto_output_ui()

    assert tag.attrs["id"] == "sales"
    assert tag.has_class("shiny-html-output")
    assert tag.has_class("html-fill-item")
    assert tag.has_class("html-fill-container")


def test_output_plotly_is_a_fill_aware_output_ui():
    tag = output_plotly("sales")

    assert tag.attrs["id"] == "sales"
    assert tag.has_class("shiny-html-output")
    assert tag.has_class("html-fill-item")
    assert tag.has_class("html-fill-container")


def test_output_plotly_with_a_height_is_a_fixed_size_fill_container():
    tag = output_plotly("sales", height="220px", width="400px")

    assert tag.has_class("html-fill-container"), "the fragment inside still fills the output"
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

        @render_plotly(figurewidget_margins=True, height="250px")
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


def test_sync_figure_renders_as_newplot_html_with_the_plotly_dependency(values):
    rendered = values["sync_fig"]

    call = parse_newplot(rendered["html"])
    assert call.data[0]["type"] == "bar"
    assert call.div_id == "sync_fig-plotly"
    assert [(d["name"], d["version"]) for d in rendered["deps"]] == [
        ("plotly", plotly.__version__),
        ("shiny-plotly", __version__),
    ]


def test_async_render_function_is_awaited(values):
    call = parse_newplot(values["async_fig"]["html"])

    assert call.layout["title"]["text"] == "async"


def test_none_renders_as_an_empty_output(values):
    assert values["empty_fig"] is None


def test_renderer_options_reach_the_fragment(values):
    html = values["parity_fig"]["html"]

    assert parse_newplot(html).layout["margin"] == {"l": 16, "t": 32, "r": 16, "b": 16}
    assert "height:250px" in html.replace(" ", "")
    assert 'class="shiny-plotly"' in html


def test_plotly_bundle_is_served_from_the_page_level_dependency():
    with TestClient(make_app()) as client:
        resp = client.get(f"/lib/plotly-{plotly.__version__}/plotly.min.js")

    assert resp.status_code == 200
    assert b"Plotly" in resp.content[:200_000]
    assert len(resp.content) > 1_000_000


def test_helper_script_is_served_once_an_output_has_rendered():
    with TestClient(make_app()) as client:
        first_flush(client)
        resp = client.get(f"/lib/shiny-plotly-{__version__}/shiny-plotly.js")

    assert resp.status_code == 200
    assert b"ResizeObserver" in resp.content
    assert b"Plotly.purge(" in resp.content


def test_plotly_bundle_is_served_from_the_rendered_dependency_without_a_page_level_call():
    app_ui = ui.page_fluid(output_plotly("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return bar()

    with (
        TestClient(App(app_ui, server)) as client,
        client.websocket_connect("/websocket/") as ws,
    ):
        ws.receive_json()
        ws.send_json({"method": "init", "data": {".clientdata_output_fig_hidden": False}})
        for _ in range(50):
            msg = ws.receive_json()
            if "values" in msg and "fig" in msg["values"]:
                break
        else:
            pytest.fail("no output values flushed after init")
        src = msg["values"]["fig"]["deps"][0]["script"][0]["src"]
        resp = client.get(f"/{src}")

    assert src == f"lib/plotly-{plotly.__version__}/plotly.min.js"
    assert resp.status_code == 200
    assert len(resp.content) > 1_000_000
