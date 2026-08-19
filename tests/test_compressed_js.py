"""Compressed, immutable serving of plotly.min.js once an app has rendered a figure."""

import gzip
import pathlib

import plotly
import plotly.graph_objects as go
import pytest
from shiny import App, Inputs, Outputs, Session, render, ui
from starlette.testclient import TestClient

from shiny_plotly import _serve, fig_to_ui, output_plotly, render_plotly

BUNDLE_URL = f"/lib/plotly-{plotly.__version__}/plotly.min.js"
RAW = (pathlib.Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_bytes()


def bar() -> go.Figure:
    return go.Figure(go.Bar(x=["a", "b"], y=[1, 2]))


def make_app() -> App:
    app_ui = ui.page_fluid(output_plotly("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return bar()

    return App(app_ui, server)


def start_session(client: TestClient, output_id: str = "fig") -> None:
    """Connect like the browser does and wait for the first flush carrying the output."""
    with client.websocket_connect("/websocket/") as ws:
        ws.receive_json()
        ws.send_json({"method": "init", "data": {f".clientdata_output_{output_id}_hidden": False}})
        for _ in range(50):
            msg = ws.receive_json()
            if "values" in msg and output_id in msg["values"]:
                return
        pytest.fail("no output values flushed after init")


@pytest.fixture(scope="module")
def client():
    with TestClient(make_app()) as client:
        start_session(client)
        assert _serve.bundle().wait(timeout=30), "compression did not finish"
        yield client


def test_bundle_is_served_gzipped_and_immutable_once_a_figure_has_rendered(client):
    resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert resp.headers["vary"] == "Accept-Encoding"
    assert resp.headers["content-type"].startswith("text/javascript")
    assert int(resp.headers["content-length"]) < len(RAW) // 2
    assert resp.content == RAW, "httpx decodes the body; it must be the exact bundle"


def test_bundle_is_served_raw_but_immutable_without_accept_encoding(client):
    resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "identity"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert int(resp.headers["content-length"]) == len(RAW)
    assert resp.content == RAW


def test_bundle_revalidation_answers_304_per_encoding(client):
    first = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip"})
    etag = first.headers["etag"]

    again = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    other = client.get(BUNDLE_URL, headers={"Accept-Encoding": "identity", "If-None-Match": etag})

    assert again.status_code == 304
    assert other.status_code == 200, "the raw body has its own ETag"
    assert other.headers["etag"] != etag


def test_brotli_is_preferred_when_the_module_is_installed(client):
    pytest.importorskip("brotli")

    resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip, br"})

    assert resp.headers["content-encoding"] == "br"
    assert resp.content == RAW


def test_head_requests_carry_the_same_headers(client):
    resp = client.head(BUNDLE_URL, headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert resp.content == b""


def test_the_route_is_installed_once_per_app(client):
    start_session(client)
    start_session(client)

    names = [getattr(r, "name", None) for r in client.app.starlette_app.router.routes]

    assert names.count(_serve.ROUTE_NAME) == 1
    assert names[0] == _serve.ROUTE_NAME, "it must shadow shiny's mount of the same path"


def test_fig_to_ui_inside_render_ui_enables_it_too():
    app_ui = ui.page_fluid(ui.output_ui("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render.ui
        def fig():
            return fig_to_ui(bar())

    with TestClient(App(app_ui, server)) as client:
        start_session(client)
        resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "identity"})

    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_the_environment_can_switch_the_route_off(monkeypatch):
    monkeypatch.setenv("SHINY_PLOTLY_NO_COMPRESS", "1")

    app = make_app()
    with TestClient(app) as client:
        start_session(client)
        names = [getattr(r, "name", None) for r in app.starlette_app.router.routes]
        resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip"})

    assert _serve.ROUTE_NAME not in names
    assert "cache-control" not in resp.headers, "shiny's own static serving, untouched"


def test_bundle_before_compression_has_finished_is_served_raw_and_cacheable():
    """The route never waits for the background compression; it serves what it has."""
    pending = _serve.CompressedBundle(_serve.bundle().path)

    resp = _serve.response_for(pending, accept_encoding="gzip, br", if_none_match=None)

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_gzip_body_is_the_bundle():
    bundle = _serve.bundle()
    assert bundle.wait(timeout=30)

    assert gzip.decompress(bundle.encodings["gzip"]) == RAW
