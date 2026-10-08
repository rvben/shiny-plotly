"""Draw with the real basic bundle in a separate server process."""

import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from plotly.offline import get_plotlyjs_version

from shiny_plotly._bundle import ENV_VAR, download_url, read_bundle

pytestmark = pytest.mark.browser
REPO = Path(__file__).parent.parent.parent
APP = """
    import plotly.graph_objects as go
    from shiny import App, ui
    from shiny_plotly import output_plotly, render_plotly

    app_ui = ui.page_fluid(output_plotly("bars"), output_plotly("histogram"))

    def server(input, output, session):
        @render_plotly
        def bars():
            return go.Figure(go.Bar(y=[1, 2, 3]))

        @render_plotly
        def histogram():
            return go.Figure(go.Histogram(x=[1, 2, 2, 3]))

    app = App(app_ui, server)
"""


@pytest.fixture(scope="module")
def basic_bundle() -> Path:
    version = get_plotlyjs_version()
    path = REPO / "tmp" / "bundles" / version / "plotly-basic.min.js"
    if not path.exists():
        request = urllib.request.Request(
            download_url("basic", version), headers={"User-Agent": "shiny-plotly-tests"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.URLError as error:
            pytest.skip(f"basic plotly.js bundle unavailable: {error}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    assert read_bundle(path).trace_types == {"scatter", "bar", "pie"}
    return path


@pytest.fixture(scope="module")
def basic_url(basic_bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    app_dir = tmp_path_factory.mktemp("basic-app")
    (app_dir / "app.py").write_text(textwrap.dedent(APP))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = app_dir / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=app_dir,
            env={**os.environ, ENV_VAR: str(basic_bundle)},
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
                    break
                except OSError:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(
                            f"basic-bundle app did not start:\n{log_path.read_text()}"
                        ) from None
                    time.sleep(0.1)
            yield f"http://127.0.0.1:{port}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def test_basic_bundle_draws_supported_traces_and_reports_missing_types(page: Page, basic_url: str):
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    scripts: list[str] = []
    page.on("request", lambda req: scripts.append(req.url) if "plotly.min.js" in req.url else None)
    page.goto(basic_url + "/")
    expect(page.locator("#bars svg.main-svg").first).to_be_attached()
    assert page.locator("#bars .bars .point").count() == 3
    assert len(scripts) == 1 and "+basic." in scripts[0], scripts
    histogram = page.locator("#histogram")
    expect(histogram).to_have_class(re.compile(r"\bshiny-output-error\b"))
    expect(histogram).to_contain_text("'histogram' is not in the basic plotly.js bundle")
    assert errors == []
