"""Bundle selection, content identity, trace validation and compressed serving."""

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import plotly
import plotly.graph_objects as go
import pytest
from plotly.offline import get_plotlyjs_version
from shiny import App, ui
from starlette.testclient import TestClient

from shiny_plotly import (
    _bundle,
    _serve,
    add_traces,
    fig_to_ui,
    output_plotly,
    plotly_js,
    render_plotly,
    restyle,
    update,
    use_plotly_bundle,
)

from helpers import run

PLOTLYJS = get_plotlyjs_version()


def fake_bundle(path, *, variant="basic", version=PLOTLYJS, traces=("scatter", "bar", "pie")):
    body = "".join(f'{{moduleType:"trace",name:"{t}"}},' for t in traces)
    path.write_text(f"/**\n * plotly.js ({variant} - minified) v{version}\n */\n{body}")
    return path


@pytest.fixture(autouse=True)
def fresh_choice(monkeypatch) -> Iterator[None]:
    monkeypatch.delenv(_bundle.ENV_VAR, raising=False)
    _bundle._reset()
    _serve._bundle = None
    yield
    # Finish a compressor before removing its source directory.
    if _serve._bundle is not None:
        assert _serve._bundle.wait(timeout=30)
    _bundle._reset()
    _serve._bundle = None


@pytest.fixture
def basic(tmp_path):
    return fake_bundle(tmp_path / "basic.js")


def test_full_bundle_is_unchanged():
    dep = plotly_js()
    assert str(dep.version) == plotly.__version__
    assert dep.source_path_map()["source"].endswith("package_data")
    _bundle.check_figure({"data": [{"type": "anything"}]})
    _bundle.check_trace_types(["anything"])
    _bundle.check_style({"type": "anything"})


def test_real_installed_bundle_metadata():
    path = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    bundle = _bundle.read_bundle(path)
    assert {"scatter", "bar", "histogram", "scatter3d"} <= bundle.trace_types
    assert bundle.variant == "full"
    assert bundle.plotlyjs_version == PLOTLYJS
    assert bundle.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "plotly.js-dist-min@" in _bundle.download_url("full", PLOTLYJS)


def test_snapshot_is_private_and_identifies_its_exact_content(basic):
    (basic.parent / "app.py").write_text("SECRET = 1")
    original = basic.read_bytes()
    use_plotly_bundle(basic)
    basic.write_text("not a bundle anymore")
    basic.unlink()
    dep = plotly_js()
    source = Path(dep.source_path_map()["source"])
    assert source != basic.parent
    assert os.listdir(source) == ["plotly.min.js"]
    assert (source / "plotly.min.js").read_bytes() == original
    assert str(dep.version) == f"{plotly.__version__}+basic.{hashlib.sha256(original).hexdigest()}"
    assert plotly_js() == dep
    _bundle._reset()
    assert not source.exists()


def test_content_change_changes_the_dependency_version(basic):
    use_plotly_bundle(basic)
    first = str(plotly_js().version)
    _bundle._reset()
    use_plotly_bundle(fake_bundle(basic, traces=("scatter",)))
    assert str(plotly_js().version) != first


def test_all_outputs_and_fragments_share_the_dependency(basic):
    use_plotly_bundle(basic)
    version = str(plotly_js().version)
    for tag in (output_plotly("fig"), fig_to_ui(go.Figure(go.Bar(y=[1])))):
        deps = [d for d in ui.TagList(tag).get_dependencies() if d.name == "plotly"]
        assert [str(d.version) for d in deps] == [version]


def test_environment_selection_and_explicit_precedence(basic, tmp_path, monkeypatch):
    monkeypatch.setenv(_bundle.ENV_VAR, str(basic))
    assert "+basic." in str(plotly_js().version)
    _bundle._reset()
    use_plotly_bundle(fake_bundle(tmp_path / "other.js", variant="cartesian"))
    assert "+cartesian." in str(plotly_js().version)


@pytest.mark.parametrize("failure", ["version", "banner", "traces"])
def test_invalid_bundle_is_refused_and_environment_can_be_repaired(tmp_path, monkeypatch, failure):
    path = fake_bundle(tmp_path / "bad.js")
    if failure == "version":
        fake_bundle(path, version="1.0.0")
        match = "installed plotly"
    elif failure == "banner":
        path.write_text('moduleType:"trace",name:"bar"')
        match = "no plotly.js banner"
    else:
        fake_bundle(path, traces=())
        match = "no trace types"
    with pytest.raises(ValueError, match=match) as raised:
        use_plotly_bundle(path)
    if failure == "version":
        assert _bundle.download_url("basic", PLOTLYJS) in str(raised.value)
    monkeypatch.setenv(_bundle.ENV_VAR, str(path))
    for _ in range(2):
        with pytest.raises(ValueError, match=match):
            plotly_js()
    fake_bundle(path)
    assert "+basic." in str(plotly_js().version)


@pytest.mark.parametrize(
    "variant,label", [("Custom Build", "custom.build"), ("", "full"), ("!!!", "custom")]
)
def test_custom_variant_is_a_valid_dependency_version(tmp_path, variant, label):
    use_plotly_bundle(fake_bundle(tmp_path / "custom.js", variant=variant))
    assert f"+{label}." in str(plotly_js().version)


def test_single_quoted_custom_registration_is_found(tmp_path):
    path = fake_bundle(tmp_path / "custom.js")
    path.write_text(path.read_text().replace('"', "'"))
    assert _bundle.read_bundle(path).trace_types == {"scatter", "bar", "pie"}


@pytest.mark.parametrize("initial_partial", [False, True])
def test_selection_is_fixed_at_first_output(basic, tmp_path, initial_partial):
    if initial_partial:
        use_plotly_bundle(basic)
    output_plotly("fig")
    if initial_partial:
        use_plotly_bundle(basic)
        other = fake_bundle(tmp_path / "other.js", traces=("bar",))
    else:
        other = basic
    with pytest.raises(RuntimeError, match="before the first plotly output"):
        use_plotly_bundle(other)


def test_copy_failure_cleans_up_and_does_not_settle(basic, monkeypatch):
    use_plotly_bundle(basic)
    write_bytes = Path.write_bytes
    directories = []

    def fail(path, content):
        directories.append(path.parent)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(OSError, match="disk full"):
        plotly_js()
    assert not _bundle.selection_settled()
    assert not directories[0].exists()
    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    assert "+basic." in str(plotly_js().version)


def test_unrelated_app_does_not_settle_choice(basic):
    app = App(ui.page_fluid("No plots"), None)
    assert not _bundle.selection_settled()
    assert _serve.enable_compressed_plotly_js(app) is False
    use_plotly_bundle(basic)
    assert "+basic." in str(plotly_js().version)


def test_callable_ui_serves_partial_bundle_compressed_before_first_session(basic):
    app = App(lambda request: ui.page_fluid(output_plotly("fig")), None)
    assert not _bundle.selection_settled()
    use_plotly_bundle(basic)
    with TestClient(app) as client:
        page = client.get("/")
        dep = plotly_js()
        href = dep.source_path_map(lib_prefix=app.lib_prefix)["href"]
        assert href in page.text
        assert _serve.bundle().wait(timeout=30)
        response = client.get(f"/{href}/plotly.min.js", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.content == basic.read_bytes()
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"] == _serve.CACHE_CONTROL


def test_static_ui_serves_partial_bundle_compressed(basic):
    use_plotly_bundle(basic)
    app = App(ui.page_fluid(output_plotly("fig")), None)
    href = plotly_js().source_path_map(lib_prefix=app.lib_prefix)["href"]
    with TestClient(app) as client:
        assert _serve.bundle().wait(timeout=30)
        response = client.get(f"/{href}/plotly.min.js", headers={"Accept-Encoding": "gzip"})
    assert response.content == basic.read_bytes()
    assert response.headers["content-encoding"] == "gzip"


def test_partial_bundle_still_works_without_compression(basic, monkeypatch):
    monkeypatch.setenv("SHINY_PLOTLY_NO_COMPRESS", "1")
    use_plotly_bundle(basic)
    app = App(ui.page_fluid(output_plotly("fig")), None)
    href = plotly_js().source_path_map(lib_prefix=app.lib_prefix)["href"]
    with TestClient(app) as client:
        response = client.get(f"/{href}/plotly.min.js", headers={"Accept-Encoding": "identity"})
    assert response.content == basic.read_bytes()
    assert "content-encoding" not in response.headers


def test_figures_and_frames_refuse_missing_types(basic):
    use_plotly_bundle(basic)
    assert fig_to_ui(go.Figure([go.Bar(y=[1]), go.Pie(values=[1]), go.Scatter(y=[1])]))
    with pytest.raises(ValueError, match="'histogram'") as raised:
        fig_to_ui(go.Figure(go.Histogram(x=[1, 2])))
    assert "bar, pie, scatter" in str(raised.value)
    fig = {"data": [{"type": "bar", "y": [1]}], "frames": [{"data": [{"y": [2]}]}]}
    assert fig_to_ui(fig)
    fig["frames"].append({"data": [{"type": "heatmap", "z": [[1]]}]})
    with pytest.raises(ValueError, match="'heatmap'"):
        fig_to_ui(fig)


def test_untyped_data_and_explicit_frame_reset_require_scatter(tmp_path):
    use_plotly_bundle(fake_bundle(tmp_path / "bars.js", traces=("bar",)))
    for fig in (
        {"data": [{"y": [1]}]},
        {"data": [{"type": "bar"}], "frames": [{"data": [{"type": None}]}]},
    ):
        with pytest.raises(ValueError, match="'scatter'"):
            fig_to_ui(fig)


def test_render_and_added_traces_are_checked(basic):
    use_plotly_bundle(basic)

    @render_plotly
    def fig(): ...

    assert run(fig.transform(go.Figure(go.Bar(y=[1]))))
    with pytest.raises(ValueError, match="'violin'"):
        run(fig.transform(go.Figure(go.Violin(y=[1]))))
    with pytest.raises(ValueError, match="'box'"):
        run(add_traces("fig", [go.Bar(y=[1]), {"type": "box"}]))


@pytest.mark.parametrize("kind", ["contour", ["bar", "contour"], ("contour", None)])
def test_restyle_and_update_refuse_missing_types(basic, kind):
    use_plotly_bundle(basic)
    for method in (restyle, update):
        with pytest.raises(ValueError, match="'contour'"):
            run(method("fig", {"type": kind}))


def test_numpy_type_updates_are_checked_after_serialization(basic):
    np = pytest.importorskip("numpy")
    use_plotly_bundle(basic)
    for kind in (np.array(["bar", "histogram"]), np.array("histogram")):
        for method in (restyle, update):
            with pytest.raises(ValueError, match="'histogram'"):
                run(method("fig", {"type": kind}))
    _bundle.check_style({"type": np.array(["bar", "scatter"])})


@pytest.mark.parametrize("kind", [None, ["bar", None], (None,), 3])
def test_type_reset_requires_scatter(tmp_path, kind):
    use_plotly_bundle(fake_bundle(tmp_path / "bars.js", traces=("bar",)))
    with pytest.raises(ValueError, match="'scatter'"):
        _bundle.check_style({"type": kind})


def test_style_omission_is_not_a_reset_and_does_not_settle_choice():
    _bundle.check_style(None)
    _bundle.check_style({"marker.color": "red"})
    assert not _bundle.selection_settled()


def test_dynamic_fragment_registers_its_route_after_app_construction(basic):
    from shiny import render

    def server(input, output, session):
        @render.ui
        def fig():
            return fig_to_ui(go.Figure(go.Bar(y=[1])))

    app = App(ui.page_fluid(ui.output_ui("fig")), server)
    assert not _bundle.selection_settled()
    use_plotly_bundle(basic)
    with TestClient(app) as client:
        with client.websocket_connect("/websocket/") as ws:
            ws.receive_json()
            ws.send_json({"method": "init", "data": {".clientdata_output_fig_hidden": False}})
            for _ in range(50):
                message = ws.receive_json()
                if "fig" in message.get("values", {}):
                    break
            else:
                pytest.fail("no dynamic figure sent")
            assert "error" not in message
        assert _serve.bundle().wait(timeout=30)
        href = plotly_js().source_path_map(lib_prefix=app.lib_prefix)["href"]
        response = client.get(f"/{href}/plotly.min.js", headers={"Accept-Encoding": "gzip"})
    assert response.content == basic.read_bytes()
    assert response.headers["content-encoding"] == "gzip"


def test_supported_updates_are_sent_with_their_serialized_types(basic):
    import json

    from shiny import reactive

    np = pytest.importorskip("numpy")
    use_plotly_bundle(basic)

    def server(input, output, session):
        @reactive.effect
        async def send_updates():
            await add_traces("fig", go.Bar(y=[1]))
            await restyle("fig", {"type": np.array(["bar", "scatter"])})
            await update("fig", {"type": [None, "pie"]})

    app = App(ui.page_fluid(output_plotly("fig")), server)
    messages = []
    with TestClient(app) as client, client.websocket_connect("/websocket/") as ws:
        ws.receive_json()
        ws.send_json({"method": "init", "data": {}})
        for _ in range(50):
            message = ws.receive_json()
            if "shiny-plotly" in message.get("custom", {}):
                messages.append(message["custom"]["shiny-plotly"])
            if len(messages) == 3:
                break
        else:
            pytest.fail("supported updates were not sent")
    assert [m["method"] for m in messages] == ["addTraces", "restyle", "update"]
    assert json.loads(messages[0]["args"])[0][0]["type"] == "bar"
    assert json.loads(messages[1]["args"])[0]["type"] == ["bar", "scatter"]
    assert json.loads(messages[2]["args"])[0]["type"] == [None, "pie"]


@pytest.mark.parametrize("variant", ["full", "Custom Build"])
def test_version_mismatch_gives_an_actionable_remedy(tmp_path, variant):
    path = fake_bundle(tmp_path / "old.js", version="1.0.0", variant=variant)
    with pytest.raises(ValueError) as raised:
        use_plotly_bundle(path)
    if variant == "full":
        assert _bundle.download_url("full", PLOTLYJS) in str(raised.value)
    else:
        assert f"Rebuild this custom bundle with plotly.js {PLOTLYJS}" in str(raised.value)
        assert "jsdelivr" not in str(raised.value)
