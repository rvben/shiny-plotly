"""A restarted worker draws a real plot from cached brotli on its first request."""

import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import plotly
import pytest
from playwright.sync_api import Page, expect

from shiny_plotly import _serve
from shiny_plotly._cache import DIRECTORY_ENV, DISABLE_ENV

pytestmark = pytest.mark.browser
APP = """
    import plotly.graph_objects as go
    from shiny import App, ui
    from shiny_plotly import output_plotly, render_plotly
    app_ui = ui.page_fluid(output_plotly("bars"))
    def server(input, output, session):
        @render_plotly
        def bars():
            return go.Figure(go.Bar(y=[1, 2, 3]))
    app = App(app_ui, server)
"""


@pytest.fixture(scope="module")
def warm_cache_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    directory = tmp_path_factory.mktemp("warm-cache-app")
    cache_dir = directory / "cache"
    path = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv(DISABLE_ENV, raising=False)
        patch.setenv(DIRECTORY_ENV, str(cache_dir))
        bundle = _serve.CompressedBundle(path)
        bundle.start()
        assert bundle.wait(timeout=30)
    (directory / "app.py").write_text(textwrap.dedent(APP))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, DIRECTORY_ENV: str(cache_dir)}
    env.pop(DISABLE_ENV, None)
    env.pop("SHINY_PLOTLY_BUNDLE", None)
    with (directory / "server.log").open("w") as log:
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
            cwd=directory,
            env=env,
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
                        raise RuntimeError((directory / "server.log").read_text()) from None
                    time.sleep(0.05)
            yield f"http://127.0.0.1:{port}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def test_new_worker_serves_cached_brotli_and_draws(page: Page, warm_cache_url: str):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    with page.expect_response(lambda response: "plotly.min.js" in response.url) as received:
        page.goto(warm_cache_url + "/")
    response = received.value
    assert response.status == 200
    assert response.headers["content-encoding"] == "br"
    assert response.headers["cache-control"] == _serve.CACHE_CONTROL
    expect(page.locator("#bars .bars .point")).to_have_count(3)
    assert errors == []
