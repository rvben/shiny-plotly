"""
Serve plotly.min.js compressed and immutable.

Shiny serves every HTML dependency through a plain static-files mount: no compression,
no ``Cache-Control``, so each visit moves the 4.8 MB bundle or at least revalidates it.
The bundle's URL is keyed by the plotly version (``/lib/plotly-<version>/plotly.min.js``),
so it can be served with a year-long ``immutable`` cache and pre-compressed once.

Shiny gives a package no hook into an ``App`` as it is built; the first hook is the
session. So the first :class:`~shiny_plotly.render_plotly` (or :func:`~shiny_plotly.fig_to_ui`
inside a session) of a process adds a route in front of Shiny's mount for the bundle's
exact path, and a background thread compresses the bundle once per process. Until it
has finished, the route serves the raw file, with the same cache headers. The page load
that started the very first session of a process has already requested the bundle
from Shiny's own mount by then; every request after it is served here.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from ._deps import plotly_js

__all__ = ("ROUTE_NAME", "CompressedBundle", "bundle", "enable_compressed_plotly_js")

ROUTE_NAME = "shiny-plotly-bundle"
CACHE_CONTROL = "public, max-age=31536000, immutable"
MEDIA_TYPE = "text/javascript; charset=utf-8"

try:
    import brotli
except ImportError:  # pragma: no cover - optional extra
    brotli = None


class CompressedBundle:
    """plotly.min.js plus its compressed encodings, produced once in the background."""

    def __init__(self, path: Path) -> None:
        self.path = path
        stat = path.stat()
        self.size = stat.st_size
        # Keyed by what is on disk, like starlette's own static ETag, so a different build
        # of the same plotly version still gets a different tag.
        self.etag_base = hashlib.sha1(
            f"{stat.st_size}-{stat.st_mtime_ns}".encode(), usedforsecurity=False
        ).hexdigest()[:16]
        self.encodings: dict[str, bytes] = {}
        self._ready = threading.Event()
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._compress, name="shiny-plotly-compress", daemon=True).start()

    def wait(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def etag(self, encoding: str | None) -> str:
        return f'"{self.etag_base}"' if encoding is None else f'"{self.etag_base}-{encoding}"'

    def _compress(self) -> None:
        raw = self.path.read_bytes()
        try:
            # brotli at quality 9 takes about as long as gzip for a quarter less output;
            # quality 11 would take thirty times longer for another 10%.
            if brotli is not None:
                self.encodings["br"] = brotli.compress(raw, quality=9)
            self.encodings["gzip"] = gzip.compress(raw, compresslevel=9, mtime=0)
        finally:
            self._ready.set()


_bundle: CompressedBundle | None = None
_bundle_lock = threading.Lock()


def bundle() -> CompressedBundle:
    """The process-wide bundle for the installed plotly; compression starts on first call."""
    global _bundle
    with _bundle_lock:
        if _bundle is None:
            paths = plotly_js().source_path_map(lib_prefix="lib/")
            _bundle = CompressedBundle(Path(paths["source"]) / "plotly.min.js")
        _bundle.start()
        return _bundle


def accepted_encodings(accept_encoding: str | None) -> Iterator[str]:
    """Encodings the client accepts, in our order of preference."""
    if not accept_encoding:
        return
    offered: set[str] = set()
    for part in accept_encoding.split(","):
        token, _, params = part.partition(";")
        if params.replace(" ", "").lower() == "q=0":
            continue
        offered.add(token.strip().lower())
    for encoding in ("br", "gzip"):
        if encoding in offered or "*" in offered:
            yield encoding


def response_for(
    bundle: CompressedBundle,
    *,
    accept_encoding: str | None,
    if_none_match: str | None,
    method: str = "GET",
) -> Response:
    encoding = next((e for e in accepted_encodings(accept_encoding) if e in bundle.encodings), None)
    etag = bundle.etag(encoding)
    headers = {"Cache-Control": CACHE_CONTROL, "Vary": "Accept-Encoding", "ETag": etag}
    if if_none_match is not None and etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)
    if encoding is None:
        # Streams the file from disk; HEAD is detected from the request scope.
        return FileResponse(bundle.path, media_type=MEDIA_TYPE, headers=headers)
    body = bundle.encodings[encoding]
    headers["Content-Encoding"] = encoding
    headers["Content-Length"] = str(len(body))
    return Response(b"" if method == "HEAD" else body, media_type=MEDIA_TYPE, headers=headers)


async def _endpoint(request: Request) -> Response:
    return response_for(
        bundle(),
        accept_encoding=request.headers.get("accept-encoding"),
        if_none_match=request.headers.get("if-none-match"),
        method=request.method,
    )


_enable_lock = threading.Lock()


def enable_compressed_plotly_js(app: object) -> bool:
    """
    Serve plotly.min.js compressed and immutable from ``app``, starting now.

    Every :class:`~shiny_plotly.render_plotly` does this for its app when its first
    session starts, which is too late for the page load that started that session. A Core
    app can call it as soon as the ``shiny.App`` exists, so the first visitor of the
    process gets the compressed bundle too::

        app = App(app_ui, server)
        enable_compressed_plotly_js(app)

    Returns True when the route was added, False when it was already there, ``app`` is
    not a Shiny app, or ``SHINY_PLOTLY_NO_COMPRESS`` is set in the environment (the escape
    hatch for deployments that want Shiny's own static serving untouched).
    """
    if os.environ.get("SHINY_PLOTLY_NO_COMPRESS"):
        return False
    starlette_app = getattr(app, "starlette_app", None)
    lib_prefix = getattr(app, "lib_prefix", None)
    if starlette_app is None or not isinstance(lib_prefix, str):
        return False
    routes = starlette_app.router.routes
    with _enable_lock:
        if any(getattr(r, "name", None) == ROUTE_NAME for r in routes):
            return False
        href = plotly_js().source_path_map(lib_prefix=lib_prefix)["href"]
        route = Route(
            "/" + href.strip("/") + "/plotly.min.js",
            _endpoint,
            methods=["GET", "HEAD"],
            name=ROUTE_NAME,
        )
        routes.insert(0, route)
    bundle()
    return True


def enable_for_current_session() -> None:
    """Enable for the app of the active session, if any (no-op outside a session)."""
    from shiny.session import get_current_session

    session = get_current_session()
    if session is not None:
        enable_compressed_plotly_js(getattr(session, "app", None))
