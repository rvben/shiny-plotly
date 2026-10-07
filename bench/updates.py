"""Full render versus a one-trace update in Chromium; see bench/README.md."""

from __future__ import annotations

import argparse
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Any, cast

import numpy as np
import plotly.graph_objects as go
import uvicorn
from playwright.sync_api import sync_playwright
from shiny import App, Inputs, Outputs, Session, reactive, ui

from shiny_plotly import output_plotly, render_plotly, update

from .drawing import capture, workload
from .realworld import SOURCES, environment, figure


def make_app(fig: go.Figure) -> App:
    data = cast(Any, fig).data
    y = np.asarray(data[0].y)

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def chart():
            n = input.full()
            fresh = go.Figure(fig)
            if n:
                cast(Any, fresh).data[0].y = y + n
                fresh.update_layout(title_text=f"Change {n}")
            return fresh

        @reactive.effect
        @reactive.event(input.delta, ignore_init=True)
        async def change():
            n = input.delta()
            await update(
                "chart",
                restyle={"y": [y + n]},
                relayout={"title.text": f"Change {n}"},
                indices=0,
            )

    return App(
        ui.page_fluid(
            ui.div(
                ui.input_action_button("full", "Full"),
                ui.input_action_button("delta", "Delta"),
                style="display:none",
            ),
            output_plotly("chart", height="450px"),
        ),
        server,
    )


def run(
    fig: go.Figure,
    repeats: int,
    profiles: Path | None = None,
    *,
    headless_shell: bool = False,
    case: str = "stocks",
) -> dict[str, Any]:
    server = uvicorn.Server(
        uvicorn.Config(make_app(fig), host="127.0.0.1", port=0, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    results: dict[str, Any] = {
        "environment": environment(),
        "repeats": repeats,
        "source": SOURCES[case.split("_")[0]],
        "case": case,
        "browser_mode": "headless-shell" if headless_shell else "new-headless",
    }
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Server startup timeout")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=None if headless_shell else "chromium")
            results["chromium_version"] = browser.version
            try:
                page = browser.new_page(viewport={"width": 1000, "height": 750})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                received = [0]
                page.on(
                    "websocket",
                    lambda ws: ws.on(
                        "framereceived",
                        lambda frame: received.__setitem__(
                            0,
                            received[0] + len(frame.encode() if isinstance(frame, str) else frame),
                        ),
                    ),
                )
                page.goto(f"http://127.0.0.1:{port}", timeout=180000)
                page.wait_for_function(
                    "document.querySelector('.plotly-graph-div')?._shinyPlotlyDrawn === true",
                    timeout=180000,
                )
                results["graphics"] = page.evaluate("""() => {
                    const gl=document.createElement('canvas').getContext('webgl');
                    const ext=gl?.getExtension('WEBGL_debug_renderer_info');
                    const renderer=ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
                    gl?.getExtension('WEBGL_lose_context')?.loseContext();
                    return {plotly_js:Plotly.version, pixel_ratio:devicePixelRatio,
                        gl_renderer:renderer};
                }""")
                page.evaluate("""() => {
                    window.measurement = null;
                    for (const method of ['react', 'update']) {
                        const original = Plotly[method];
                        Plotly[method] = function() {
                            const start = performance.now();
                            return Promise.resolve(original.apply(this, arguments)).then(value => {
                                window.measurement = {method, plotly_ms: performance.now()-start,
                                    interaction_ms: performance.now()-window.sent};
                                return value;
                            });
                        };
                    }
                }""")

                def change(mode: str, n: int) -> dict[str, Any]:
                    before = received[0]
                    page.evaluate(
                        """args => {
                            window.measurement = null; window.sent = performance.now();
                            Shiny.setInputValue(args[0],args[1],{priority:'event'});
                        }""",
                        [mode, n],
                    )
                    page.wait_for_function("window.measurement !== null", timeout=180000)
                    row = page.evaluate("window.measurement")
                    assert row["method"] == ("react" if mode == "full" else "update")
                    row["received_bytes"] = received[0] - before
                    drawn = page.evaluate("""() => {
                        const gd = document.querySelector('.plotly-graph-div');
                        return {y:gd._fullData[0].y[0], title:gd.layout.title.text};
                    }""")
                    expected = float(cast(Any, fig).data[0].y[0]) + n
                    assert drawn["y"] == expected or (
                        (drawn["y"] is None or math.isnan(drawn["y"])) and math.isnan(expected)
                    )
                    assert drawn["title"] == f"Change {n}"
                    return row

                samples: dict[str, list[dict[str, Any]]] = {"full": [], "delta": []}
                # Alternate paths so neither benefits systematically from a warmer page.
                change_number = 0
                for iteration in range(repeats + 1):
                    for mode in ("full", "delta") if iteration % 2 == 0 else ("delta", "full"):
                        change_number += 1
                        row = change(mode, change_number)
                        if iteration:
                            samples[mode].append(row)
                for mode, rows in samples.items():
                    results[mode] = {
                        key: statistics.median(row[key] for row in rows)
                        for key in ("interaction_ms", "plotly_ms", "received_bytes")
                    }
                if profiles:
                    results["profiles"] = {}
                    for mode in ("full", "delta"):
                        change_number += 1
                        results["profiles"][mode] = capture(
                            page,
                            lambda mode=mode, n=change_number: change(mode, n),
                            profiles / f"shiny-{case}-{mode}",
                        )
                results["page_errors"] = errors
                if errors:
                    raise RuntimeError(f"Browser errors: {errors}")
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", choices=("stocks", "power_regular", "power_regular_steps"), default="stocks"
    )
    parser.add_argument("--cache", type=Path, default=Path("/tmp/shiny-plotly-realworld"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--headless-shell", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    import json

    fig = (
        figure(args.cache, "stocks")
        if args.case == "stocks"
        else workload(args.cache, args.case)[0]
    )
    result = run(
        fig,
        args.repeats,
        args.profiles,
        headless_shell=args.headless_shell,
        case=args.case,
    )
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
