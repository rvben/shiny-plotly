"""Full render versus a one-trace update in Chromium; see bench/README.md."""

from __future__ import annotations

import argparse
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


def run(fig: go.Figure, repeats: int) -> dict[str, Any]:
    server = uvicorn.Server(
        uvicorn.Config(make_app(fig), host="127.0.0.1", port=0, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    results: dict[str, Any] = {
        "environment": environment(),
        "repeats": repeats,
        "source": SOURCES["stocks"],
    }
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Server startup timeout")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
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
                samples: dict[str, list[dict[str, Any]]] = {"full": [], "delta": []}
                # Alternate paths so neither benefits systematically from a warmer page.
                change_number = 0
                for iteration in range(repeats + 1):
                    for mode in ("full", "delta") if iteration % 2 == 0 else ("delta", "full"):
                        change_number += 1
                        before = received[0]
                        page.evaluate(
                            """args => {
                                window.measurement = null; window.sent = performance.now();
                                Shiny.setInputValue(args[0],args[1],{priority:'event'});
                            }""",
                            [mode, change_number],
                        )
                        page.wait_for_function("window.measurement !== null", timeout=180000)
                        row = page.evaluate("window.measurement")
                        assert row["method"] == ("react" if mode == "full" else "update")
                        row["received_bytes"] = received[0] - before
                        drawn = page.evaluate("""() => {
                            const gd = document.querySelector('.plotly-graph-div');
                            return {y:gd._fullData[0].y[0], title:gd.layout.title.text};
                        }""")
                        assert drawn["y"] == float(cast(Any, fig).data[0].y[0]) + change_number
                        assert drawn["title"] == f"Change {change_number}"
                        if iteration:
                            samples[mode].append(row)
                for mode, rows in samples.items():
                    results[mode] = {
                        key: statistics.median(row[key] for row in rows)
                        for key in ("interaction_ms", "plotly_ms", "received_bytes")
                    }
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
    parser.add_argument("--cache", type=Path, default=Path("/tmp/shiny-plotly-realworld"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    import json

    result = run(figure(args.cache, "stocks"), args.repeats)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
