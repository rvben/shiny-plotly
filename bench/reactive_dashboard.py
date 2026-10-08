"""Measure normal Shiny slider reactivity through the latest correct dashboard paint."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import plotly
import plotly.graph_objects as go
import shiny
import uvicorn
from playwright.sync_api import CDPSession, Page, sync_playwright
from plotly.offline import get_plotlyjs_version
from shiny import App, Inputs, Outputs, Session, ui
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from shiny_plotly import output_plotly, render_plotly


def figure(index: int, revision: int, points: int, traces: int) -> go.Figure:
    x = np.arange(points)
    return go.Figure(
        [
            go.Scatter(
                x=x,
                y=np.sin(x / 30 + trace * 0.7 + index * 0.15 + revision * 0.05)
                * (1 + revision / 50)
                + trace * 2
                + index * 0.02,
                mode="lines",
                name=f"Series {trace + 1}",
            )
            for trace in range(traces)
        ],
        layout={
            "title": {"text": f"Chart {index + 1}: window {revision}"},
            "meta": {"revision": revision, "index": index},
            "uirevision": "dashboard",
            "template": None,
            "showlegend": False,
            "margin": {"l": 35, "r": 10, "t": 30, "b": 25},
        },
    )


def make_app(args: argparse.Namespace, coalesce: bool) -> App:
    page = ui.page_fluid(
        ui.tags.head(ui.tags.link(rel="icon", href="data:,")),
        ui.input_slider(
            "revision",
            "Time window",
            min=0,
            max=args.steps,
            value=0,
            step=1,
            animate={"interval": args.interval_ms, "loop": False},
        ),
        ui.div(
            *(
                output_plotly(f"chart{i}", height="160px", coalesce_renders=coalesce)
                for i in range(args.charts)
            ),
            style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px",
        ),
    )

    def server(input: Inputs, output: Outputs, session: Session) -> None:
        builds: list[dict[str, Any]] = []
        latest: dict[int, int] = {}
        reported = False

        def register(index: int) -> None:
            @output(id=f"chart{index}")
            @render_plotly
            def chart():
                revision = int(input.revision())
                start = time.perf_counter()
                fig = figure(index, revision, args.points, args.traces)
                builds.append(
                    {
                        "chart": index,
                        "revision": revision,
                        "ms": (time.perf_counter() - start) * 1000,
                    }
                )
                latest[index] = revision
                return fig

        for index in range(args.charts):
            register(index)

        async def report() -> None:
            nonlocal reported
            if (
                reported
                or len(latest) != args.charts
                or any(revision != args.steps for revision in latest.values())
            ):
                return
            reported = True
            # Public telemetry only. Figures use the ordinary renderer/flush/transport.
            await session.send_custom_message("reactive-bench", {"builds": builds})

        session.on_flushed(report, once=False)

    return App(page, server)


INSTRUMENT = """() => {
    const b = window.reactiveBench = {
        changes: [], sent: [], received: [], draws: [], stats: null,
        figureBytes: 0, lastInputAt: null, done: {}, started: performance.now()
    };
    const slider = $('#revision');
    b.ratePolicy = slider.data('shiny-input-binding').getRatePolicy(slider[0]);
    slider.on('change.bench', () => {
        const revision = slider.data('ionRangeSlider').result.from;
        if (b.changes.length && b.changes[b.changes.length-1].revision === revision) return;
        b.lastInputAt = performance.now();
        b.changes.push({revision, at: b.lastInputAt});
    });
    Shiny.addCustomMessageHandler('reactive-bench', data => { b.stats = data; });
    const socket = Shiny.shinyapp.$socket, send = socket.send;
    socket.send = function(payload) {
        const message = JSON.parse(payload);
        if (message.data?.revision !== undefined)
            b.sent.push({revision: message.data.revision, at: performance.now()});
        return send.apply(this, arguments);
    };
    socket.addEventListener('message', event => {
        const message = JSON.parse(event.data);
        for (const [id, value] of Object.entries(message.values || {})) {
            if (!id.startsWith('chart')) continue;
            const fig = JSON.parse(value.figure);
            b.figureBytes += new TextEncoder().encode(value.figure).length;
            b.received.push({id, revision: fig.layout.meta.revision, at: performance.now()});
        }
    });
    const react = Plotly.react;
    Plotly.react = function(gd, fig) {
        const row = {id: gd.id, revision: fig.layout.meta.revision, start: performance.now()};
        b.draws.push(row);
        const result = react.apply(this, arguments);
        result.then(() => {
            row.end = performance.now(); b.done[gd.id] = row.revision;
        });
        return result;
    };
}"""

# Widget updates emit normal slider changes and retain Shiny's native debounce policy.
# Animation is driven by clicking Shiny's own Play button, without overriding its timer.
DRAG = """({steps, interval}) => new Promise(resolve => {
    const el = $('#revision'), slider = el.data('ionRangeSlider');
    let revision = 0;
    function next() {
        slider.update({from: ++revision});
        el.trigger('change');
        if (revision === steps) resolve();
        else setTimeout(next, interval);
    }
    next();
})"""

FINAL = """({charts, steps}) => {
    const b = window.reactiveBench;
    return b.stats && Object.keys(b.done).length === charts &&
        Object.values(b.done).every(revision => revision === steps) &&
        $('#revision').data('ionRangeSlider').result.from === steps &&
        !$('#revision').data('animating');
}"""

VERIFY = """({charts, steps, points, traces}) => {
    const graphs = [...document.querySelectorAll('.plotly-graph-div')];
    return graphs.length === charts && graphs.every((g, index) =>
        g._shinyPlotlyDrawn && g.layout.meta.revision === steps &&
        g._fullData.length === traces && g._fullData.every((trace, t) => {
            const x = Array.from(trace.x), y = Array.from(trace.y);
            if (x.length !== points || y.length !== points) return false;
            return y.every((v, j) => x[j] === j && Math.abs(v - (
                Math.sin(j/30 + t*.7 + index*.15 + steps*.05) * (1 + steps/50)
                + t*2 + index*.02)) < 1e-8);
        }));
}"""


def metrics(client: CDPSession) -> dict[str, float]:
    return {row["name"]: row["value"] for row in client.send("Performance.getMetrics")["metrics"]}


def sample(page: Page, client: CDPSession, args: argparse.Namespace, mode: str) -> dict[str, Any]:
    before = metrics(client)
    page.evaluate(INSTRUMENT)
    if mode == "drag":
        page.evaluate(DRAG, {"steps": args.steps, "interval": args.interval_ms})
    else:
        page.evaluate(
            "() => { $('.slider-animate-button[data-target-id=revision]').trigger('click'); }"
        )
    params = {"charts": args.charts, "steps": args.steps}
    page.wait_for_function(FINAL, arg=params, timeout=120_000)
    page.evaluate("() => shinyPlotly.flush()")
    completed = page.evaluate("""() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now()))))""")
    after = metrics(client)
    assert page.evaluate(VERIFY, {**params, "points": args.points, "traces": args.traces}), (
        "latest dashboard data differs"
    )
    row: dict[str, Any] = page.evaluate(
        """end => {
        const b = reactiveBench;
        const builds = b.stats.builds.filter(r => r.revision > 0);
        return {
            total_ms: end - b.started,
            after_last_input_ms: end - b.lastInputAt,
            input_span_ms: b.lastInputAt - b.changes[0].at,
            input_changes: b.changes, inputs_sent: b.sent,
            figures_received: b.received.length, received: b.received,
            figure_payload_bytes: b.figureBytes,
            server_builds: builds.length,
            server_build_ms: builds.reduce((sum, r) => sum + r.ms, 0),
            draws: b.draws.length, draw_log: b.draws,
            rate_policy: b.ratePolicy
        };
    }""",
        completed,
    )
    assert [r["revision"] for r in row["input_changes"]] == list(range(1, args.steps + 1))
    assert row["inputs_sent"][-1]["revision"] == args.steps
    assert row["server_builds"] >= args.charts
    assert row["figures_received"] == row["server_builds"], "built figures did not reach browser"
    row["browser_task_ms"] = (after["TaskDuration"] - before["TaskDuration"]) * 1000
    row["browser_script_ms"] = (after["ScriptDuration"] - before["ScriptDuration"]) * 1000
    row["browser_layout_ms"] = (after["LayoutDuration"] - before["LayoutDuration"]) * 1000
    return row


def percentile(values: list[float], p: float) -> float:
    """Linearly interpolate the empirical percentile; keep raw samples alongside it."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = {
        f"{mode}-{'coalesced' if coalesce else 'default'}": (mode, coalesce)
        for mode in ("drag", "animation")
        for coalesce in (False, True)
    }
    http = uvicorn.Server(
        uvicorn.Config(
            Starlette(
                routes=[
                    Mount(f"/{coalesce}", app=make_app(args, coalesce))
                    for coalesce in (False, True)
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
        "scope": "normal reactive renderers; native slider policies; local WebSocket transport",
        "checkpoint": "latest data in every graph, then flush and two animation frames",
        "cpu_metric": "CDP main-thread task duration; includes benchmark instrumentation",
        "viewport": {"width": 1200, "height": 950},
        "grid": {"columns": 4, "chart_height_px": 160},
        "percentile_method": "linear empirical p95; sample size limits tail precision",
        "cases": {},
    }
    try:
        deadline = time.monotonic() + 30
        while not http.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("reactive dashboard server did not start")
            time.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium")
            result["chromium"] = browser.version
            try:
                for repeat in range(args.repeats):
                    order = list(cases)
                    if repeat % 2:
                        order.reverse()
                    for name in order:
                        mode, coalesce = cases[name]
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
                            client = page.context.new_cdp_session(page)
                            client.send("Emulation.setCPUThrottlingRate", {"rate": args.cpu})
                            client.send("Performance.enable")
                            page.goto(f"http://127.0.0.1:{port}/{coalesce}/")
                            page.wait_for_function(
                                """charts => {
                                const gs = [...document.querySelectorAll('.plotly-graph-div')];
                                return gs.length === charts && gs.every(g => g._shinyPlotlyDrawn);
                            }""",
                                arg=args.charts,
                            )
                            row = sample(page, client, args, mode)
                            assert not errors, errors
                            rows[name].append(row)
                            print(
                                f"{repeat + 1}/{args.repeats} {name}: "
                                f"{row['total_ms']:.1f}ms total, "
                                f"{row['after_last_input_ms']:.1f}ms after last input, "
                                f"{row['server_builds']} builds, {row['draws']} draws",
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
            raise RuntimeError("reactive dashboard server did not stop")
    fields = ("total_ms", "after_last_input_ms", "browser_task_ms", "draws", "server_builds")
    result["cases"] = {
        name: {
            "mode": cases[name][0],
            "coalesce_renders": cases[name][1],
            "summary": {
                field: {
                    "median": statistics.median(r[field] for r in samples),
                    "p95": percentile([r[field] for r in samples], 0.95),
                }
                for field in fields
            },
            "samples": samples,
        }
        for name, samples in rows.items()
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charts", type=int, default=12)
    parser.add_argument("--points", type=int, default=1000)
    parser.add_argument("--traces", type=int, default=2)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        min(
            args.charts,
            args.points,
            args.traces,
            args.steps,
            args.interval_ms,
            args.repeats,
            args.cpu,
        )
        < 1
    ):
        parser.error("counts, interval and CPU rate must be positive")
    result = run(args)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Chromium {result['chromium']}; {args.cpu}x CPU control; median / empirical p95")
    for name, row in result["cases"].items():
        summary = row["summary"]
        print(
            f"{name}: total {summary['total_ms']['median']:.1f} / "
            f"{summary['total_ms']['p95']:.1f}ms; after last input "
            f"{summary['after_last_input_ms']['median']:.1f} / "
            f"{summary['after_last_input_ms']['p95']:.1f}ms; "
            f"{summary['draws']['median']} draws / {summary['server_builds']['median']} builds"
        )


if __name__ == "__main__":
    main()
