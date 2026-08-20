"""Run the browser test apps on a real uvicorn server for the playwright tests."""

import importlib.util
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page
from shiny import App
from starlette.applications import Starlette
from starlette.routing import Mount

from .apps import (
    make_app,
    make_dark_app,
    make_events_app,
    make_lazy_app,
    make_live_app,
    make_theme_app,
)


def load_example(name: str) -> App:
    """The ``app`` of an example file, imported as is: the tests drive the real example."""
    path = Path(__file__).parent.parent.parent / "examples" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


@pytest.fixture(scope="session")
def server_url() -> Iterator[str]:
    root = Starlette(
        routes=[
            Mount("/lazy", app=make_lazy_app()),
            Mount("/events", app=make_events_app()),
            Mount("/live", app=make_live_app()),
            Mount("/dark", app=make_dark_app()),
            Mount("/theme", app=make_theme_app()),
            Mount("/stream", app=load_example("streaming_app.py")),
            Mount("/", app=make_app()),
        ]
    )
    server = uvicorn.Server(uvicorn.Config(root, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def errors(page: Page) -> Iterator[list[str]]:
    """Console errors and uncaught exceptions raised while a test drives the page."""
    seen: list[str] = []
    page.on("pageerror", lambda err: seen.append(f"pageerror: {err}"))
    page.on("console", lambda msg: seen.append(msg.text) if msg.type == "error" else None)
    yield seen
