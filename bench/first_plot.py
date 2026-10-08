"""Measure navigation to a drawn chart in Chromium, with cold/warm HTTP caches."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue
import statistics
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go
import shiny
import uvicorn
from playwright.sync_api import Browser, sync_playwright
from shiny import App, ui

from shiny_plotly import _serve, output_plotly, render_plotly, use_plotly_bundle
from shiny_plotly._cache import DIRECTORY_ENV, DISABLE_ENV

# Observe actual SVG bars, then allow a rendering opportunity. This is a paint
# checkpoint, not proof of a physically presented frame or a GPU completion fence.
OBSERVER = """(() => {
    let pending = false;
    const observer = new MutationObserver(() => {
        const bars = [...document.querySelectorAll('#fig .bars .point path')];
        if (pending || bars.length !== 3 || bars.some(bar => {
            const r = bar.getBoundingClientRect();
            return r.width <= 0 || r.height <= 0;
        })) return;
        pending = true;
        observer.disconnect();
        window.firstPlotDomMs = performance.now();
        requestAnimationFrame(() => requestAnimationFrame(() => {
            window.firstPlotPaintMs = performance.now();
        }));
    });
    observer.observe(document, {childList:true, subtree:true, attributes:true});
})();"""


def serve(directory: str, bundle: str | None, ready: Any, stop: Any) -> None:
    os.environ[DIRECTORY_ENV] = directory
    for name in (DISABLE_ENV, "SHINY_PLOTLY_NO_COMPRESS", "SHINY_PLOTLY_BUNDLE"):
        os.environ.pop(name, None)
    if bundle:
        use_plotly_bundle(bundle)

    def server(input: Any, output: Any, session: Any) -> None:
        @render_plotly
        def fig():
            return go.Figure(go.Bar(y=[1, 2, 3]), layout={"template": None})

    app = App(ui.page_fluid(output_plotly("fig", height="400px")), server)
    if not _serve.bundle().wait(timeout=30):
        raise RuntimeError("compression did not finish")
    http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not http.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        ready.put(f"http://127.0.0.1:{port}/")
        if not stop.wait(timeout=300):
            raise TimeoutError("benchmark controller did not stop server")
    finally:
        http.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("server did not stop")


@contextmanager
def running_server(directory: Path, bundle: Path | None):
    context = multiprocessing.get_context("spawn")
    ready, stop = context.Queue(), context.Event()
    process = context.Process(
        target=serve,
        args=(str(directory), str(bundle) if bundle else None, ready, stop),
    )
    process.start()
    try:
        deadline = time.monotonic() + 65
        while True:
            try:
                url = ready.get(timeout=0.25)
                break
            except queue.Empty:
                if process.exitcode is not None:
                    raise RuntimeError(f"server exited with {process.exitcode}") from None
                if time.monotonic() > deadline:
                    raise TimeoutError("server startup timed out") from None
        yield url
    finally:
        stop.set()
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        ready.close()
        ready.join_thread()
    if process.exitcode != 0:
        raise RuntimeError(f"server exited with {process.exitcode}")


def pair(browser: Browser, url: str, throttled: bool) -> list[dict[str, Any]]:
    context = browser.new_context(viewport={"width": 1000, "height": 750})
    try:
        context.add_init_script(OBSERVER)
        page = context.new_page()
        page.set_default_timeout(60_000)
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4 if throttled else 1})
        cdp.send(
            "Network.emulateNetworkConditions",
            {
                "offline": False,
                "latency": 80 if throttled else 0,
                "downloadThroughput": 1_600_000 / 8 if throttled else -1,
                "uploadThroughput": 750_000 / 8 if throttled else -1,
            },
        )
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        rows = []
        for phase in ("cold", "warm"):
            page.goto(url, wait_until="commit", timeout=60_000)
            page.wait_for_function("Number.isFinite(window.firstPlotPaintMs)")
            row = page.evaluate("""() => {
                const resources = performance.getEntriesByType('resource').filter(
                    r => new URL(r.name).pathname.endsWith('/plotly.min.js'));
                if (resources.length !== 1) throw Error('Expected exactly one Plotly asset');
                const r = resources[0];
                if (document.querySelector('.shiny-output-error')) throw Error('Output failed');
                const gd = document.querySelector('#fig .plotly-graph-div');
                if (gd.data[0].y.join(',') !== '1,2,3')
                    throw Error('Unexpected chart values');
                return {dom_ms:firstPlotDomMs, paint_ms:firstPlotPaintMs,
                    plotly_download_ms:r.duration,
                    after_plotly_response_ms:firstPlotDomMs-r.responseEnd,
                    plotly_transfer_bytes:r.transferSize,
                    plotly_encoded_bytes:r.encodedBodySize, plotly_decoded_bytes:r.decodedBodySize,
                    plotly_cached:r.transferSize === 0 && r.decodedBodySize > 0,
                    plotly_js:Plotly.version};
            }""")
            if errors:
                raise RuntimeError(f"browser errors: {errors}")
            if row["plotly_cached"] != (phase == "warm"):
                raise RuntimeError(f"unexpected {phase} browser cache state: {row}")
            row["phase"] = phase
            rows.append(row)
            # A fresh document navigation retains HTTP cache without reload's
            # revalidation semantics. This also terminates the old Shiny session.
            page.goto("about:blank")
        return rows
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basic-bundle", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--headless-shell", action="store_true")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    bundle = args.basic_bundle.resolve()
    if not bundle.is_file():
        parser.error("--basic-bundle must name an existing matching Plotly.js bundle")
    samples: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory(prefix="shiny-first-plot-") as directory,
        sync_playwright() as pw,
    ):
        browser = pw.chromium.launch(channel=None if args.headless_shell else "chromium")
        version = browser.version
        try:
            for index in range(args.rounds):
                variants = [("full", None), ("basic", bundle)]
                if index % 2:
                    variants.reverse()
                for variant, path in variants:
                    with running_server(Path(directory) / variant, path) as url:
                        modes = [False, True] if index % 2 == 0 else [True, False]
                        for throttled in modes:
                            for row in pair(browser, url, throttled):
                                samples.append(
                                    {
                                        **row,
                                        "variant": variant,
                                        "throttled": throttled,
                                        "round": index,
                                    }
                                )
        finally:
            browser.close()
    result = {
        "chromium": version,
        "browser_mode": "headless-shell" if args.headless_shell else "new-headless",
        "python": sys.version,
        "platform": sys.platform,
        "plotly_python": plotly.__version__,
        "shiny": shiny.__version__,
        "rounds": args.rounds,
        "throttle": {
            "cpu_rate": 4,
            "latency_ms": 80,
            "download_bps": 1_600_000,
            "upload_bps": 750_000,
        },
        "scope": "navigation to three SVG bars plus two rAF; server compression already ready",
        "samples": samples,
    }
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Chromium {version}; medians of {args.rounds} rounds; server compression ready.")
    print("bundle | mode | cache | chart checkpoint ms | Plotly download ms | transfer bytes")
    for variant in ("full", "basic"):
        for throttled in (False, True):
            for phase in ("cold", "warm"):
                rows = [
                    r
                    for r in samples
                    if r["variant"] == variant
                    and r["throttled"] == throttled
                    and r["phase"] == phase
                ]
                values = [
                    statistics.median(r[key] for r in rows)
                    for key in ("paint_ms", "plotly_download_ms", "plotly_transfer_bytes")
                ]
                print(
                    f"{variant} | {'throttled' if throttled else 'normal'} | {phase} | "
                    + " | ".join(f"{value:.2f}" for value in values)
                )


if __name__ == "__main__":
    main()
