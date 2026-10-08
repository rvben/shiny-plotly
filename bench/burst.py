"""Compare snapshot coalescing over the real Shiny WebSocket in Chromium."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import plotly
import shiny
import uvicorn
from playwright.sync_api import sync_playwright
from plotly.offline import get_plotlyjs_version
from shiny import App, Inputs, Outputs, Session, reactive, ui
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from shiny_plotly import output_plotly, render_plotly


def figure(index: int, revision: int, points: int) -> dict[str, Any]:
    return {
        "data": [
            {
                "type": "scatter",
                "mode": "lines",
                "x": list(range(points)),
                "y": [(j * 13 + index) % 101 + revision for j in range(points)],
            }
        ],
        "layout": {
            "title": {"text": str(revision)},
            "template": None,
            "margin": {"l": 35, "r": 10, "t": 30, "b": 25},
        },
    }


def make_app(args: argparse.Namespace, stacked: bool, coalesce: bool) -> App:
    page = ui.page_fluid(
        ui.tags.head(ui.tags.link(rel="icon", href="data:,")),
        ui.input_action_button("burst", "Burst"),
        ui.div(
            *(
                output_plotly(
                    f"chart{i}",
                    height="260px" if stacked else "160px",
                    defer_offscreen=stacked,
                    coalesce_renders=coalesce,
                )
                for i in range(args.charts)
            ),
            style=f"display:grid;grid-template-columns:repeat({2 if stacked else 4},1fr);gap:8px",
        ),
    )

    def server(input: Inputs, output: Outputs, session: Session) -> None:
        def register(index: int) -> None:
            @output(id=f"chart{index}")
            @render_plotly
            def chart():
                return figure(index, 0, args.points)

        for index in range(args.charts):
            register(index)

        @reactive.effect
        @reactive.event(input.burst, ignore_init=True)
        async def burst() -> None:
            # Fixed snapshots isolate browser backpressure from server reactive batching.
            # This private transport is used only by the benchmark, never by the library.
            for revision in range(1, args.snapshots + 1):
                await session._send_message(
                    {
                        "values": {
                            f"chart{i}": {
                                "figure": json.dumps(figure(i, revision, args.points)),
                                "config": {"responsive": True},
                            }
                            for i in range(args.charts)
                        }
                    }
                )
                await asyncio.sleep(args.interval_ms / 1000)

    return App(page, server)


INSTRUMENT = """options => {
    window.burstLog = []; window.latestReceived = 0; window.burstFrames = 0;
    window.burstDone = {};
    Shiny.shinyapp.$socket.addEventListener('message', event => {
        const message = JSON.parse(event.data);
        if (message.values?.chart0) {
            latestReceived = Number(JSON.parse(message.values.chart0.figure).layout.title.text);
            burstFrames++;
        }
    });
    const react = Plotly.react;
    let held = false;
    Plotly.react = function(gd, fig) {
        const revision = Number(fig.layout.title.text);
        burstLog.push({id: gd.id, revision, latest_at_start: latestReceived});
        const call = () => react.call(Plotly, gd, fig).then(() => {
            burstDone[gd.id] = revision;
        });
        if (options.hold_ms && !held) {
            held = true;
            return new Promise(r => setTimeout(r, options.hold_ms)).then(call);
        }
        return call();
    };
    if (options.hold_idle) window.requestIdleCallback = () => 1;
    window.burstStarted = performance.now();
    Shiny.setInputValue('burst', 1, {priority: 'event'});
}"""


def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = {
        f"{layout}-{'coalesced' if coalesce else 'default'}": (layout == "stacked", coalesce)
        for layout in ("visible", "stacked")
        for coalesce in (False, True)
    }
    http = uvicorn.Server(
        uvicorn.Config(
            Starlette(
                routes=[
                    Mount("/" + name, app=make_app(args, *options))
                    for name, options in cases.items()
                ]
                + [Route("/favicon.ico", lambda request: Response(status_code=204))]
            ),
            host="127.0.0.1",
            port=0,
            log_level="error",
        )
    )
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in cases}
    result: dict[str, Any] = {
        "python": sys.version,
        "platform": sys.platform,
        "plotly_python": plotly.__version__,
        "plotly_js": get_plotlyjs_version(),
        "shiny": shiny.__version__,
        "options": {k: v for k, v in vars(args).items() if k != "output"},
        "scope": "warm figures; fixed WebSocket snapshots; flush then two rAF; local transport",
        "stacked_idle": "held until flush",
        "cases": {},
    }
    try:
        deadline = time.monotonic() + 30
        while not http.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("burst server did not start")
            time.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium")
            result["chromium"] = browser.version
            try:
                for repeat in range(args.repeats):
                    # Reverse every other repeat to reduce systematic case-order bias.
                    order = list(cases)
                    if repeat % 2:
                        order.reverse()
                    for name in order:
                        page = browser.new_page(viewport={"width": 1200, "height": 950})
                        errors: list[str] = []
                        page.on("pageerror", lambda err, seen=errors: seen.append(str(err)))
                        page.on(
                            "console",
                            lambda msg, seen=errors: (
                                seen.append(msg.text) if msg.type == "error" else None
                            ),
                        )
                        try:
                            page.context.new_cdp_session(page).send(
                                "Emulation.setCPUThrottlingRate", {"rate": args.cpu}
                            )
                            page.goto(f"http://127.0.0.1:{port}/{name}/")
                            page.wait_for_function(
                                """n => {
                                const graphs = [...document.querySelectorAll('.plotly-graph-div')];
                                return graphs.length === n &&
                                    graphs.every(g => g._shinyPlotlyDrawn);
                            }""",
                                arg=args.charts,
                            )
                            page.evaluate(
                                INSTRUMENT,
                                {"hold_ms": args.hold_first_ms, "hold_idle": cases[name][0]},
                            )
                            page.wait_for_function(
                                "n => burstDone['chart0-plotly'] === n && burstFrames === n",
                                arg=args.snapshots,
                                timeout=60_000,
                            )
                            page.evaluate("() => shinyPlotly.flush()")
                            page.wait_for_function(
                                """({charts, snapshots}) =>
                                Object.keys(burstDone).length === charts &&
                                Object.values(burstDone).every(n => n === snapshots)
                            """,
                                arg={"charts": args.charts, "snapshots": args.snapshots},
                                timeout=60_000,
                            )
                            elapsed = page.evaluate("""() => new Promise(resolve =>
                                requestAnimationFrame(() => requestAnimationFrame(() =>
                                    resolve(performance.now() - burstStarted))))""")
                            assert page.evaluate(
                                """n =>
                                [...document.querySelectorAll('.plotly-graph-div')].every((g, i) =>
                                    g._fullData[0].y[0] === i % 101 + n)
                            """,
                                args.snapshots,
                            ), "final snapshot data differs"
                            assert not errors, errors
                            sample = page.evaluate(
                                """() => ({
                                draws: burstLog.length, frames: burstFrames,
                                obsolete_at_start: burstLog.filter(
                                    e => e.revision < e.latest_at_start).length,
                                first_revisions: burstLog.filter(
                                    e => e.id === 'chart0-plotly').map(e => e.revision),
                                last_revisions: burstLog.filter(
                                    e => e.id === 'chart"""
                                + str(args.charts - 1)
                                + """-plotly')
                                    .map(e => e.revision)
                            })"""
                            )
                            sample["all_ms"] = elapsed
                            rows[name].append(sample)
                            print(
                                f"{repeat + 1}/{args.repeats} {name}: "
                                f"{elapsed:.1f}ms, {sample['draws']} draws",
                                flush=True,
                            )
                        finally:
                            page.close()
            finally:
                browser.close()
    finally:
        http.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("burst server did not stop")
    result["cases"] = {
        name: {
            "defer_offscreen": cases[name][0],
            "coalesce_renders": cases[name][1],
            "median_all_ms": statistics.median(r["all_ms"] for r in samples),
            "median_draws": statistics.median(r["draws"] for r in samples),
            "samples": samples,
        }
        for name, samples in rows.items()
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charts", type=int, default=20)
    parser.add_argument("--points", type=int, default=500)
    parser.add_argument("--snapshots", type=int, default=5)
    parser.add_argument("--interval-ms", type=float, default=20)
    parser.add_argument("--hold-first-ms", type=float, default=0)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.charts, args.points, args.snapshots, args.repeats, args.cpu) < 1:
        parser.error("charts, points, snapshots, repeats and cpu must be positive")
    if args.interval_ms < 0 or args.hold_first_ms < 0:
        parser.error("interval and hold must be nonnegative")
    result = run(args)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Chromium {result['chromium']}; {args.cpu}x CPU control; median all-chart completion")
    for name, row in result["cases"].items():
        print(f"{name}: {row['median_all_ms']:.1f}ms, {row['median_draws']} draws")


if __name__ == "__main__":
    main()
