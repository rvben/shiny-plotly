"""Measure many-chart redraw latency, offscreen deferral and resize work in Chromium."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go
import shiny
import uvicorn
from playwright.sync_api import sync_playwright
from plotly.offline import get_plotlyjs_version
from shiny import App, Inputs, Outputs, Session, ui

from shiny_plotly import output_plotly, render_plotly

HANDLERS = """(() => {
    window.dashboardResizeHandlers = new Set();
    const wrappers = new Map();
    const add = window.addEventListener, remove = window.removeEventListener;
    window.addEventListener = function(type, fn, ...rest) {
        if (type !== 'resize' || typeof fn !== 'function')
            return add.call(this, type, fn, ...rest);
        if (!wrappers.has(fn)) wrappers.set(fn, function(...args) {
            if (window.dashboardCalls &&
                [...document.querySelectorAll('.plotly-graph-div')].some(
                    gd => gd._responsiveChartHandler === fn)) dashboardCalls.native_resize++;
            return fn.apply(this,args);
        });
        dashboardResizeHandlers.add(fn);
        return add.call(this, type, wrappers.get(fn), ...rest);
    };
    window.removeEventListener = function(type, fn, ...rest) {
        if (type === 'resize') dashboardResizeHandlers.delete(fn);
        return remove.call(this, type,
            type === 'resize' && wrappers.has(fn) ? wrappers.get(fn) : fn, ...rest);
    };
})();"""

INSTRUMENT = """() => {
    window.dashboardCalls = {react:0, resize:0, relayout:0, native_resize:0};
    const react = Plotly.react, resize = Plotly.Plots.resize;
    Plotly.react = function(...args) {
        dashboardCalls.react++;
        const result = react.apply(this,args);
        result.then(() => args[0]._dashboardRevision = args[1].layout.title.text);
        return result;
    };
    Plotly.Plots.resize = function(...args) {
        dashboardCalls.resize++;
        return resize.apply(this,args);
    };
    for (const gd of document.querySelectorAll('.plotly-graph-div')) {
        gd._dashboardRevision = gd.layout.title.text;
        gd.on('plotly_relayout', () => dashboardCalls.relayout++);
    }
}"""


def make_app(charts: int, points: int, stacked: bool, deferred: bool) -> App:
    height = "260px" if stacked else "160px"
    columns = 2 if stacked else 4
    page = ui.page_fluid(
        ui.tags.head(ui.tags.link(rel="icon", href="data:,")),
        ui.input_action_button("refresh", "Refresh"),
        ui.div(
            *(
                output_plotly(f"chart{i}", height=height, defer_offscreen=deferred)
                for i in range(charts)
            ),
            id="dashboard",
            style=f"display:grid;grid-template-columns:repeat({columns},1fr);gap:8px",
        ),
    )

    def server(input: Inputs, output: Outputs, session: Session) -> None:
        def register(index: int) -> None:
            @output(id=f"chart{index}")
            @render_plotly
            def chart():
                n = input.refresh()
                return go.Figure(
                    go.Scatter(
                        x=list(range(points)),
                        y=[(j * 13 + index) % 101 + n for j in range(points)],
                        mode="lines",
                    ),
                    layout={
                        "template": None,
                        "title": {"text": str(n)},
                        "margin": {"l": 35, "r": 10, "t": 30, "b": 25},
                    },
                )

        for index in range(charts):
            register(index)

    return App(page, server)


CURRENT = """n => [...document.querySelectorAll('.plotly-graph-div')].every(
    gd => gd._shinyPlotlyDrawn && gd.layout.title.text === String(n))"""
MATCHES = """() => [...document.querySelectorAll('.plotly-graph-div')].every(gd => {
    const r = gd.getBoundingClientRect();
    return Math.abs(gd._fullLayout.width-r.width)<=1 &&
        Math.abs(gd._fullLayout.height-r.height)<=1;
})"""


def run(
    charts: int,
    points: int,
    repeats: int,
    cpu: int,
    headless_shell: bool,
    hold_idle: bool,
    reverse_order: bool,
) -> dict[str, Any]:
    apps = []
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    for stacked in (False, True):
        for deferred in (False, True):
            name = f"{'stacked' if stacked else 'visible'}-{deferred}"
            apps.append(Mount("/" + name, app=make_app(charts, points, stacked, deferred)))
    http = uvicorn.Server(
        uvicorn.Config(
            Starlette(
                routes=[*apps, Route("/favicon.ico", lambda request: Response(status_code=204))]
            ),
            host="127.0.0.1",
            port=0,
            log_level="error",
        )
    )
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()
    results: dict[str, Any] = {
        "python": sys.version,
        "platform": sys.platform,
        "plotly_python": plotly.__version__,
        "plotly_js": get_plotlyjs_version(),
        "shiny": shiny.__version__,
        "charts": charts,
        "points": points,
        "repeats": repeats,
        "cpu_rate": cpu,
        "scope": "warm draws; visible checkpoint plus two rAF; includes server and local transport",
        "idle_scheduler": "held" if hold_idle else "native",
        "browser_mode": "headless-shell" if headless_shell else "new-headless",
        "cases": {},
    }
    try:
        deadline = time.monotonic() + 30
        while not http.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("dashboard server did not start")
            time.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=None if headless_shell else "chromium")
            results["chromium"] = browser.version
            try:
                order = [
                    f"{layout}-{deferred}"
                    for layout in ("visible", "stacked")
                    for deferred in (False, True)
                ]
                if reverse_order:
                    order.reverse()
                results["case_order"] = order
                for name in order:
                    page = browser.new_page(viewport={"width": 1200, "height": 950})
                    try:
                        page.add_init_script(HANDLERS)
                        errors: list[str] = []
                        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                        page.on(
                            "console",
                            lambda msg, errors=errors: (
                                errors.append(msg.text) if msg.type == "error" else None
                            ),
                        )
                        page.on(
                            "response",
                            lambda response, errors=errors: (
                                errors.append(f"HTTP {response.status}: {response.url}")
                                if response.status >= 400
                                else None
                            ),
                        )
                        cdp = page.context.new_cdp_session(page)
                        cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu})
                        page.goto(f"http://127.0.0.1:{port}/{name}/")
                        page.wait_for_function(
                            'n => document.querySelectorAll(".plotly-graph-div").length === n',
                            arg=charts,
                        )
                        page.wait_for_function(CURRENT, arg=0)
                        page.wait_for_function(MATCHES)
                        page.evaluate(INSTRUMENT)
                        # A controlled idle scheduler isolates foreground responsiveness.
                        # flush below measures the remaining work rather than dropping it.
                        if hold_idle:
                            page.evaluate("window.requestIdleCallback = () => 1")
                        samples = []
                        for iteration in range(repeats + 1):
                            n = iteration + 1
                            row = page.evaluate(
                                """async n => {
                                const graphs=[...document.querySelectorAll('.plotly-graph-div')];
                                const visible=graphs.filter(gd => {
                                    const r=gd.getBoundingClientRect();
                                    return r.top<innerHeight && r.bottom>0;
                                });
                                window.dashboardCalls={react:0,resize:0,relayout:0,native_resize:0};
                                const start=performance.now();
                                Shiny.setInputValue('refresh',n,{priority:'event'});
                                async function waitFor(predicate) {
                                    while (!predicate()) {
                                        if (performance.now()-start>30000)
                                            throw Error('dashboard update timed out');
                                        await new Promise(r=>setTimeout(r,0));
                                    }
                                }
                                await waitFor(() => visible.every(
                                    gd => gd._dashboardRevision===String(n)));
                                await new Promise(r=>requestAnimationFrame(
                                    ()=>requestAnimationFrame(r)));
                                const visibleMs=performance.now()-start;
                                const foreground={...dashboardCalls};
                                await shinyPlotly.flush();
                                await waitFor(() => graphs.every(
                                    gd => gd._dashboardRevision===String(n)));
                                const allMs=performance.now()-start;
                                graphs.forEach((gd,i) => {
                                    if (gd._fullData[0].y[0] !== (i%101)+n)
                                        throw Error('Dashboard values did not update');
                                });
                                return {visible_ms:visibleMs,all_ms:allMs,
                                    visible_charts:visible.length,foreground,
                                    all:{...dashboardCalls}};
                            }""",
                                n,
                            )
                            page.wait_for_function(CURRENT, arg=n)
                            if iteration:
                                samples.append(row)
                        resize = []
                        for width in (1100, 1200):
                            page.evaluate(
                                "window.dashboardCalls={react:0,resize:0,relayout:0,native_resize:0}"
                            )
                            started = time.perf_counter()
                            page.set_viewport_size({"width": width, "height": 950})
                            page.wait_for_function(MATCHES)
                            page.wait_for_timeout(150)
                            resize.append(
                                {
                                    "ms": (time.perf_counter() - started) * 1000,
                                    **page.evaluate("dashboardCalls"),
                                }
                            )
                        if errors:
                            raise RuntimeError(f"{name}: {errors}")
                        results["cases"][name] = {
                            "samples": samples,
                            "resize": resize,
                            "native_resize_handlers": page.evaluate("""() =>
                                [...document.querySelectorAll('.plotly-graph-div')].filter(
                                    gd => dashboardResizeHandlers.has(
                                        gd._responsiveChartHandler)).length"""),
                            "median_visible_ms": statistics.median(
                                r["visible_ms"] for r in samples
                            ),
                            "median_all_ms": statistics.median(r["all_ms"] for r in samples),
                        }
                    finally:
                        page.close()
            finally:
                browser.close()
    finally:
        http.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("dashboard server did not stop")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charts", type=int, default=20)
    parser.add_argument("--points", type=int, default=500)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--headless-shell", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hold-idle", action="store_true")
    parser.add_argument("--reverse-order", action="store_true")
    args = parser.parse_args()
    if min(args.charts, args.points, args.repeats, args.cpu) < 1:
        parser.error("counts and CPU rate must be positive")
    result = run(
        args.charts,
        args.points,
        args.repeats,
        args.cpu,
        args.headless_shell,
        args.hold_idle,
        args.reverse_order,
    )
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Chromium {result['chromium']}; {args.charts} charts; {args.cpu}x CPU control")
    print("case | visible ms | all ms | total resize requests | native resize handlers")
    for name, row in result["cases"].items():
        print(
            f"{name} | {row['median_visible_ms']:.2f} | {row['median_all_ms']:.2f} | "
            f"{[r['resize'] + r['native_resize'] for r in row['resize']]} | "
            f"{row['native_resize_handlers']}"
        )


if __name__ == "__main__":
    main()
