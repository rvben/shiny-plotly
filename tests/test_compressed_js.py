"""Compressed, immutable serving of plotly.min.js, from every app's first request on."""

import gzip
import importlib.metadata
import logging
import pathlib
import threading
import types

import brotli
import httpx2._decoders
import plotly
import plotly.graph_objects as go
import pytest
from packaging.requirements import Requirement
from shiny import App, Inputs, Outputs, Session, render, ui
from shiny.express import wrap_express_app
from starlette.testclient import TestClient

from shiny_plotly import (
    _serve,
    enable_compressed_plotly_js,
    fig_to_ui,
    output_plotly,
    render_plotly,
)

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


def strip_route(app: App) -> None:
    """Take the route back out, leaving the app as one that was built before the import."""
    routes = app.starlette_app.router.routes
    routes[:] = [r for r in routes if getattr(r, "name", None) != _serve.ROUTE_NAME]


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


@pytest.fixture
def brotli_body_as_it_arrived(monkeypatch):
    """Hand a ``br`` response over undecoded, so the test reads the bytes off the wire.

    httpx decodes ``br`` by calling the installed brotli with ``output_buffer_limit``, which
    brotli only accepts from 1.2 on, so against brotli 1.1 (the floor this package declares)
    the call raises before a test sees the response. Dropping the decoder is what httpx itself
    does when brotli is missing, and what is under test is the encoding shiny-plotly serves,
    not a client's ability to decode it; a browser doing that is covered by tests/browser.
    """
    monkeypatch.delitem(httpx2._decoders.SUPPORTED_DECODERS, "br", raising=False)


def test_bundle_is_served_gzipped_and_immutable(client):
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


def test_brotli_is_the_encoding_a_browser_gets(client, brotli_body_as_it_arrived):
    """brotli is a dependency, so this is what every install serves, not a lucky one."""
    resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip, br"})

    assert resp.headers["content-encoding"] == "br"
    assert len(resp.content) < len(RAW) // 3
    assert brotli.decompress(resp.content) == RAW, "brotli of the exact bundle"


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
    """The fallback for an app the constructor did not reach: the session installs it."""
    app_ui = ui.page_fluid(ui.output_ui("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render.ui
        def fig():
            return fig_to_ui(bar())

    app = App(app_ui, server)
    strip_route(app)
    with TestClient(app) as client:
        start_session(client)
        resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "identity"})

    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_an_app_serves_the_compressed_bundle_before_any_session_exists(brotli_body_as_it_arrived):
    """No call, no websocket: the browser asks for the bundle while the page is still loading."""
    app = make_app()
    names = [getattr(r, "name", None) for r in app.starlette_app.router.routes]
    assert _serve.bundle().wait(timeout=30), "compression starts with the app, not with a session"

    with TestClient(app) as client:
        resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip, br"})

    assert names[0] == _serve.ROUTE_NAME, "installed as the app was built"
    assert resp.headers["content-encoding"] == "br"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert int(resp.headers["content-length"]) < len(RAW) // 2


def test_an_express_app_gets_the_route_too(tmp_path):
    """Express builds the same App, so the same wrapper covers it."""
    app_file = tmp_path / "app.py"
    app_file.write_text(
        "import plotly.graph_objects as go\n"
        "from shiny_plotly import render_plotly\n"
        "@render_plotly\n"
        "def fig():\n"
        "    return go.Figure(go.Bar(x=['a'], y=[1]))\n"
    )

    app = wrap_express_app(app_file)

    names = [getattr(r, "name", None) for r in app.starlette_app.router.routes]
    assert names[0] == _serve.ROUTE_NAME


def test_an_app_subclass_is_given_the_route_once():
    class Subclassed(App):
        pass

    app = Subclassed(ui.page_fluid(output_plotly("fig")), lambda i, o, s: None)

    names = [getattr(r, "name", None) for r in app.starlette_app.router.routes]
    assert names.count(_serve.ROUTE_NAME) == 1


def test_the_constructor_is_wrapped_once_however_often_the_package_is_imported():
    assert _serve.enable_for_new_apps() is False, "shiny_plotly's own import already did it"


def test_a_failure_installing_the_route_leaves_the_app_buildable(monkeypatch, caplog):
    """The app never asked for the route, so nothing about it may keep the app from existing."""

    def boom(app: object) -> bool:
        raise RuntimeError("no bundle to serve")

    monkeypatch.setattr(_serve, "enable_compressed_plotly_js", boom)

    with caplog.at_level(logging.WARNING, logger=_serve.logger.name):
        app = make_app()

    names = [getattr(r, "name", None) for r in app.starlette_app.router.routes]
    assert _serve.ROUTE_NAME not in names, "Shiny's own static mount serves it, as before"
    assert len(caplog.records) == 1, "and the deployment is told why"
    assert caplog.records[0].exc_info is not None, "with the traceback that explains it"


def test_an_app_built_before_the_package_was_imported_can_still_be_given_the_route():
    """The one case the constructor cannot reach; the public function is still the way in."""
    app = make_app()
    strip_route(app)

    assert enable_compressed_plotly_js(app) is True
    assert enable_compressed_plotly_js(app) is False, "already there; nothing added twice"
    assert _serve.bundle().wait(timeout=30)
    with TestClient(app) as client:
        resp = client.get(BUNDLE_URL, headers={"Accept-Encoding": "gzip"})

    assert resp.headers["content-encoding"] == "gzip"
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


def test_under_pyodide_the_route_is_skipped_and_the_app_still_renders(monkeypatch):
    """shinylive runs apps in pyodide, which cannot start threads and serves its own
    static assets; the compression route must bow out instead of crashing the session."""

    def no_threads(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    # Confined to _serve's namespace: the test client itself needs real threads.
    monkeypatch.setattr(_serve, "sys", types.SimpleNamespace(platform="emscripten"), raising=False)
    monkeypatch.setattr(
        _serve,
        "threading",
        types.SimpleNamespace(Thread=no_threads, Event=threading.Event, Lock=threading.Lock),
    )

    app = make_app()
    assert enable_compressed_plotly_js(app) is False
    with TestClient(app) as client:
        start_session(client)  # the render decorator enables it per session; must not crash
        names = [getattr(r, "name", None) for r in app.starlette_app.router.routes]

    assert _serve.ROUTE_NAME not in names


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


@pytest.mark.parametrize(
    ("accept_encoding", "accepted"),
    [
        ("gzip, br", ["br", "gzip"]),
        ("br", ["br"]),
        ("gzip", ["gzip"]),
        ("GZIP,  Br ", ["br", "gzip"]),
        ("*", ["br", "gzip"]),
        ("gzip;q=1.0, br;q=0.8", ["br", "gzip"]),
        ("gzip, br;q=0", ["gzip"]),
        ("gzip, br;q=0.0", ["gzip"]),
        ("gzip, br;q=0.000", ["gzip"]),
        ("*;q=0", []),
        ("identity", []),
        ("deflate, zstd", []),
        ("br;q=none-of-that", ["br"]),
        ("", []),
        (None, []),
    ],
)
def test_the_encodings_a_client_will_take_are_read_from_its_header(accept_encoding, accepted):
    """Nothing yielded means the raw file: the route has no encoding the client accepts."""
    assert list(_serve.accepted_encodings(accept_encoding)) == accepted


def stub_bundle(tmp_path: pathlib.Path) -> _serve.CompressedBundle:
    """A bundle over a small stand-in file, so a test compresses bytes rather than 4.8 MB."""
    path = tmp_path / "plotly.min.js"
    path.write_bytes(b"!function(){window.Plotly={}}();\n" * 100)
    return _serve.CompressedBundle(path)


def test_serving_the_larger_encoding_is_announced_when_brotli_is_missing(
    tmp_path, monkeypatch, caplog
):
    """Otherwise nothing about a deployment says it is shipping a quarter more bytes."""
    monkeypatch.setattr(_serve, "brotli", None)
    bundle = stub_bundle(tmp_path)

    with caplog.at_level(logging.WARNING, logger=_serve.logger.name):
        bundle.start()
        assert bundle.wait(timeout=30)

    assert set(bundle.encodings) == {"gzip"}
    assert len(caplog.records) == 1, "said once per process, not once per request"
    message = caplog.records[0].getMessage()
    assert "Install brotli" in message, "the fix is named"
    assert f"{_serve.BROTLI_SAVING_PERCENT}%" in message
    assert "logging.getLogger('shiny_plotly')" in message, "and so is the way to silence it"


def test_brotli_ships_with_the_package_but_not_where_pyodide_runs_it():
    """A deployment gets the smaller encoding without asking; a shinylive export does not.

    Under pyodide the route is never installed (the bundle comes from the export), so an
    unmarked dependency would put a 307 kB wasm wheel into every exported app for nothing.
    """
    requirements = [Requirement(text) for text in importlib.metadata.requires("shiny-plotly") or []]
    brotli_requirement = next(r for r in requirements if r.name == "brotli")

    assert brotli_requirement.marker is not None, "unmarked, so pyodide pays for it"
    assert brotli_requirement.marker.evaluate({"sys_platform": "linux"})
    assert not brotli_requirement.marker.evaluate({"sys_platform": "emscripten"})


def test_nothing_is_announced_when_brotli_is_there(tmp_path, caplog):
    bundle = stub_bundle(tmp_path)

    with caplog.at_level(logging.WARNING, logger=_serve.logger.name):
        bundle.start()
        assert bundle.wait(timeout=30)

    assert set(bundle.encodings) == {"br", "gzip"}
    assert caplog.records == []
