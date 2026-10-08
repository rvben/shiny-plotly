"""Measure first asset requests and cache readiness in fresh cold and warm workers.

Run `make bench-compression`, or pass COMPRESSION_ARGS="--bundle plotly-basic.min.js".
Timings exclude Python imports and measure local HTTP, not browser evaluation or WAN latency.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import queue
import statistics
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn
from shiny import App, ui

from shiny_plotly import _serve as serving
from shiny_plotly import output_plotly, plotly_js, use_plotly_bundle
from shiny_plotly._cache import DIRECTORY_ENV, DISABLE_ENV


def worker(directory: str, bundle_path: str | None, results: Any) -> None:
    os.environ[DIRECTORY_ENV] = directory
    for name in (DISABLE_ENV, "SHINY_PLOTLY_NO_COMPRESS", "SHINY_PLOTLY_BUNDLE"):
        os.environ.pop(name, None)
    if bundle_path is not None:
        use_plotly_bundle(bundle_path)
    started = time.perf_counter()
    app = App(ui.page_fluid(output_plotly("fig")), None)
    setup_ms = (time.perf_counter() - started) * 1000
    ready_at_setup = serving.bundle().ready
    asset_href = plotly_js().source_path_map(lib_prefix=app.lib_prefix)["href"]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("benchmark server did not start")
            time.sleep(0.001)
        port = server.servers[0].sockets[0].getsockname()[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/{asset_href}/plotly.min.js",
            headers={"Accept-Encoding": "br, gzip", "User-Agent": "shiny-plotly-benchmark"},
        )
        requested = time.perf_counter()
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            encoding = response.headers.get("Content-Encoding", "raw")
        request_ms = (time.perf_counter() - requested) * 1000
        first_response_ms = (time.perf_counter() - started) * 1000
        assert serving.bundle().wait(timeout=30)
        ready_ms = setup_ms if ready_at_setup else (time.perf_counter() - started) * 1000
        results.put(
            {
                "setup_ms": setup_ms,
                "ready_ms": ready_ms,
                "first_response_ms": first_response_ms,
                "request_ms": request_ms,
                "wire_bytes": len(body),
                "encoding": encoding,
            }
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("benchmark server did not stop")


def run_worker(context: Any, directory: Path, bundle_path: str | None) -> dict[str, Any]:
    results = context.Queue()
    process = context.Process(target=worker, args=(str(directory), bundle_path, results))
    process.start()
    try:
        deadline = time.monotonic() + 45
        while True:
            try:
                result = results.get(timeout=0.25)
                break
            except queue.Empty:
                if process.exitcode is not None:
                    raise RuntimeError(f"worker exited with {process.exitcode}") from None
                if time.monotonic() > deadline:
                    raise TimeoutError("benchmark worker timed out") from None
        process.join(timeout=15)
        if process.exitcode != 0:
            raise RuntimeError(f"worker exited with {process.exitcode}")
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        results.close()
        results.join_thread()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    bundle_path = None if args.bundle is None else str(args.bundle.resolve())
    context = multiprocessing.get_context("spawn")
    samples: dict[str, list[dict[str, Any]]] = {"cold": [], "warm": []}
    with tempfile.TemporaryDirectory(prefix="shiny-plotly-compression-bench-") as directory:
        for index in range(args.rounds):
            cache = Path(directory) / str(index)
            for phase in samples:
                result = run_worker(context, cache, bundle_path)
                if phase == "warm" and result["encoding"] == "raw":
                    raise RuntimeError("warm worker did not serve a cached encoding")
                samples[phase].append(result)
    print("Timings exclude Python imports; first response uses local HTTP.")
    print("phase | setup ms | ready ms | first response ms | HTTP ms | wire bytes | encoding")
    for phase, rows in samples.items():
        values = [
            statistics.median(row[key] for row in rows)
            for key in ("setup_ms", "ready_ms", "first_response_ms", "request_ms", "wire_bytes")
        ]
        print(
            f"{phase} | "
            + " | ".join(f"{value:.2f}" for value in values)
            + " | "
            + ",".join(sorted({row["encoding"] for row in rows}))
        )


if __name__ == "__main__":
    main()
