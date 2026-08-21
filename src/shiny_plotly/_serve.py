"""
Serve plotly.min.js compressed and immutable.

Shiny serves every HTML dependency through a plain static-files mount: no compression,
no ``Cache-Control``, so each visit moves the 4.8 MB bundle or at least revalidates it.
The bundle's URL is keyed by the plotly version (``/lib/plotly-<version>/plotly.min.js``),
so it can be served with a year-long ``immutable`` cache and pre-compressed once.

Importing ``shiny_plotly`` wraps ``shiny.App.__init__``, so every app built after it gets a
route for the bundle's exact path in front of Shiny's mount, and a background thread starts
compressing the bundle once per process. Both happen while the app is being built, before it
can serve anything, so the first request of the process is already served here; until the
compression has finished the route serves the raw file with the same cache headers. An app
built before the import can still ask for the route with :func:`enable_compressed_plotly_js`,
and each session enables it for its own app, so it is there either way.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import logging
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from ._deps import plotly_js

__all__ = (
    "ROUTE_NAME",
    "CompressedBundle",
    "bundle",
    "enable_compressed_plotly_js",
    "enable_for_new_apps",
)

ROUTE_NAME = "shiny-plotly-bundle"
CACHE_CONTROL = "public, max-age=31536000, immutable"
MEDIA_TYPE = "text/javascript; charset=utf-8"

logger = logging.getLogger(__name__)

# How much smaller brotli q9 is than gzip -9 on plotly.min.js (1.22 MB against 1.46 MB on
# plotly 6.9.0). Only used to size the warning below, which is raised precisely when brotli
# is absent and the real figure cannot be measured.
BROTLI_SAVING_PERCENT = 17

try:
    import brotli
except ImportError:  # pragma: no cover - a dependency, absent only where it was left out
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
            if brotli is None:
                # Compression runs once per process, so this is said once. Without it a
                # deployment has no way to notice it is serving the larger encoding: the
                # bundle is compressed, cached and immutable either way, just bigger.
                logger.warning(
                    "shiny-plotly is serving plotly.min.js gzipped (%.2f MB); brotli would "
                    "be about %d%% smaller. Install brotli for it, or silence this with "
                    "logging.getLogger('shiny_plotly').setLevel(logging.ERROR).",
                    len(self.encodings["gzip"]) / 1e6,
                    BROTLI_SAVING_PERCENT,
                )
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

    Importing ``shiny_plotly`` already does this for every ``shiny.App`` built afterwards,
    so an app needs no call of its own. It stays the way in for an app that was constructed
    before the import, and it is what each session calls for its own app.

    Returns True when the route was added, False when it was already there, ``app`` is
    not a Shiny app, or ``SHINY_PLOTLY_NO_COMPRESS`` is set in the environment (the escape
    hatch for deployments that want Shiny's own static serving untouched). Under pyodide
    (shinylive) it is False too: threads cannot start there, and the browser loads assets
    from the shinylive bundle rather than over HTTP, so there is nothing to compress.
    """
    if os.environ.get("SHINY_PLOTLY_NO_COMPRESS") or sys.platform == "emscripten":
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


# Marks the wrapper below, so a second import (or a module reload) wraps nothing twice.
_WRAPPED = "_shiny_plotly_wrapped"


def enable_for_new_apps() -> bool:
    """
    Serve the compressed bundle from every ``shiny.App`` built from now on.

    Wraps ``shiny.App.__init__`` once, at import of this package, and enables the route on
    each app it builds. Shiny offers a package no other hook into an app that early, and
    early is the point: the browser asks for plotly.min.js while the page is loading, long
    before the session that page opens exists, so a route installed by the first session is
    installed one visitor too late. Express constructs the same ``App``, so it is covered too.

    Returns True when the wrapper was installed, False when it was already there.
    """
    from shiny import App

    original = App.__init__
    if getattr(original, _WRAPPED, False):
        return False

    @functools.wraps(original)
    def __init__(self: App, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        try:
            enable_compressed_plotly_js(self)
        except Exception:
            # This app never asked for the route, so nothing here may keep it from being
            # built; without it the bundle is served by Shiny's own mount, as before.
            logger.warning("shiny-plotly could not serve plotly.min.js compressed", exc_info=True)

    setattr(__init__, _WRAPPED, True)
    App.__init__ = __init__
    return True


def enable_for_current_session() -> None:
    """Enable for the app of the active session, if any (no-op outside a session)."""
    from shiny.session import get_current_session

    session = get_current_session()
    if session is not None:
        enable_compressed_plotly_js(getattr(session, "app", None))
