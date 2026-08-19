"""Run the browser test apps on a real uvicorn server for the playwright tests."""

import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from .apps import make_app, make_lazy_app


@pytest.fixture(scope="session")
def server_url() -> Iterator[str]:
    root = Starlette(routes=[Mount("/lazy", app=make_lazy_app()), Mount("/", app=make_app())])
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
