"""Isolate Plotly drawing and capture Chromium CPU/timeline profiles."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np
import plotly.graph_objects as go
from playwright.sync_api import Page, sync_playwright
from plotly.offline import get_plotlyjs

from shiny_plotly import render_plotly

from .realworld import SOURCES, environment, figure

CASES = (
    "stocks_gl",
    "stocks_svg",
    "stocks_fixed",
    "stocks_sampled",
    "stocks_grouped",
    "power_gl",
    "power_regular",
    "power_regular_steps",
    "power_sampled",
    "power_sampled_svg",
    "building_sampled",
    "building_sampled_svg",
)


def workload(cache: Path, case: str) -> tuple[go.Figure, int | None, int]:
    name = case.split("_")[0]
    fig = figure(cache, name, native=True)
    data = cast(Any, fig).data
    changed_points = len(data[0].y)
    if "regular" in case:
        # Compare the same longest constant-cadence interval from the real meter.
        dates = np.asarray(data[0].x, dtype="datetime64[us]")
        deltas = np.diff(dates).astype(np.int64)
        steps, counts = np.unique(deltas, return_counts=True)
        step = steps[np.argmax(counts)]
        edges = np.r_[0, np.flatnonzero(deltas != step) + 1, len(dates)]
        longest = int(np.argmax(np.diff(edges)))
        start, stop = int(edges[longest]), int(edges[longest + 1])
        assert step == 60_000_000 and stop - start == 47_908
        for trace in data:
            np.testing.assert_array_equal(np.asarray(trace.x, dtype="datetime64[us]"), dates)
            trace.x = dates[start:stop]
            trace.y = trace.y[start:stop]
            if case.endswith("steps"):
                trace.x0 = str(dates[start])
                trace.dx = float(step) / 1000
                trace.x = None
        fig.update_xaxes(type="date")
        changed_points = stop - start
    if name == "stocks":
        # Grouping only makes sense for identically styled, non-toggleable lines.
        fig.update_traces(line_color="#636efa", line_width=1)
    if case.endswith("_grouped"):
        traces = []
        for start in range(0, len(data), 50):
            group = data[start : start + 50]
            x = np.concatenate([np.append(trace.x, np.datetime64("NaT", "ns")) for trace in group])
            y = np.concatenate([np.append(trace.y, np.nan) for trace in group])
            labels = [label for trace in group for label in [trace.name] * (len(trace.x) + 1)]
            traces.append(
                go.Scattergl(
                    x=x,
                    y=y,
                    mode="lines",
                    line={"color": "#636efa", "width": 1},
                    customdata=labels,
                    hovertemplate="%{customdata}: %{x}, %{y}<extra></extra>",
                )
            )
        fig = go.Figure(traces, layout=fig.layout)
    if case.endswith("svg"):
        fig = go.Figure(
            [
                go.Scatter(**{k: v for k, v in trace.to_plotly_json().items() if k != "type"})
                for trace in cast(Any, fig).data
            ],
            layout=fig.layout,
        )
    if case == "stocks_fixed":
        # A deliberate application control, not a safe automatic library default.
        values = np.concatenate([trace.y for trace in data])
        fig.update_yaxes(range=[float(np.nanmin(values)) - 5, float(np.nanmax(values)) + 5])
    budget = (200 if name == "stocks" else 2000) if "sampled" in case else None
    return fig, budget, changed_points


INSTALL = """args => {
    window.baseFigure = args[0]; window.changedPoints = args[1];
    window.gd = document.getElementById('chart');
    return Plotly.newPlot(gd, JSON.parse(baseFigure)).then(() => {
        window.baseY = Array.from(gd._fullData[0].y);
        window.otherY = gd._fullData[1].y[0];
        window.count = 0;
    });
}"""

OPERATION = """async mode => {
    const n = ++window.count;
    const y = mode === 'relayout' ? null :
        baseY.map((v,i) => i < changedPoints && v !== null ? v+n : v);
    const title = `Change ${n}`;
    let fresh;
    if (mode === 'react') {
        fresh = JSON.parse(baseFigure);
        fresh.data[0].y = y; fresh.layout.title = {text:title};
    }
    // Payload parsing and constructing the changed array are outside the draw timer.
    const start = performance.now();
    if (mode === 'react') await Plotly.react(gd, fresh);
    else if (mode === 'update') await Plotly.update(gd, {y:[y]}, {'title.text':title}, [0]);
    else await Plotly.relayout(gd, {'title.text':title});
    const ms = performance.now()-start;
    if (gd.layout.title.text !== title) throw Error('Title did not update');
    const expected = baseY[0] === null ? null : baseY[0]+n;
    if (mode !== 'relayout' && !Object.is(gd._fullData[0].y[0], expected))
        throw Error('Changed trace did not update');
    if (!Object.is(gd._fullData[1].y[0], otherY)) throw Error('Unchanged trace changed');
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return {promise_ms:ms, after_two_frames_ms:performance.now()-start};
}"""


def capture(page: Page, operation: Callable[[], Any], path: Path) -> dict[str, Any]:
    """Profile separately from latency samples; these timings are not speed evidence."""
    cdp = page.context.new_cdp_session(page)
    events: list[dict[str, Any]] = []
    done = []
    profiling = tracing = False
    cdp.on("Tracing.dataCollected", lambda data: events.extend(data["value"]))
    cdp.on("Tracing.tracingComplete", lambda _: done.append(True))
    try:
        cdp.send("Profiler.enable")
        cdp.send("Profiler.setSamplingInterval", {"interval": 1000})
        cdp.send("Tracing.start", {"categories": "devtools.timeline,blink.user_timing"})
        tracing = True
        cdp.send("Profiler.start")
        profiling = True
        operation()
        page.evaluate("""() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
        profile = cdp.send("Profiler.stop")["profile"]
        profiling = False
        cdp.send("Tracing.end")
        tracing = False
        deadline = time.monotonic() + 10
        while not done:
            if time.monotonic() > deadline:
                raise RuntimeError("Browser trace completion timeout")
            page.wait_for_timeout(20)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".cpuprofile").write_text(json.dumps(profile))
        path.with_suffix(".trace.json").write_text(json.dumps({"traceEvents": events}))
        return {
            "cpu_profile": str(path.with_suffix(".cpuprofile")),
            "timeline": str(path.with_suffix(".trace.json")),
        }
    finally:
        try:
            if profiling:
                cdp.send("Profiler.stop")
        finally:
            try:
                if tracing:
                    cdp.send("Tracing.end")
            finally:
                cdp.detach()


def run(
    cache: Path,
    cases: list[str],
    repeats: int,
    profiles: Path | None,
    *,
    headless_shell: bool = False,
) -> dict[str, Any]:
    results: dict[str, Any] = {
        "environment": environment(),
        "repeats": repeats,
        "case_order": cases,
        "sources": SOURCES,
        "scope": (
            "Plotly promise duration and two-rAF checkpoint; "
            "excludes server, transport and payload preparation"
        ),
        "cases": {},
        "browser_mode": "headless-shell" if headless_shell else "new-headless",
    }
    prepared = []
    for case in cases:
        fig, budget, changed_points = workload(cache, case)
        payload = asyncio.run(render_plotly(resample=budget).transform(fig))
        assert payload is not None
        encoded = str(cast(dict[str, Any], payload)["figure"])
        prepared.append((case, budget, changed_points, encoded))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=None if headless_shell else "chromium")
        results["chromium_version"] = browser.version
        try:
            for case, budget, changed_points, encoded in prepared:
                page = browser.new_page(viewport={"width": 1000, "height": 750})
                try:
                    errors: list[str] = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    page.set_content('<div id="chart" style="width:1000px;height:450px"></div>')
                    page.add_script_tag(content=get_plotlyjs() + "\n//# sourceURL=plotly-local.js")
                    page.evaluate(INSTALL, [encoded, changed_points])
                    details = page.evaluate("""() => ({
                        plotly_js:Plotly.version, traces:gd.data.length,
                        drawn_points:gd._fullData.reduce((n,t)=>n+t.y.length,0),
                        pixel_ratio:devicePixelRatio,
                        gl_renderer:(() => {
                            const gl = document.createElement('canvas').getContext('webgl');
                            if (!gl) return null;
                            const ext = gl.getExtension('WEBGL_debug_renderer_info');
                            const renderer = ext ?
                                gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
                            gl.getExtension('WEBGL_lose_context')?.loseContext();
                            return renderer;
                        })()
                    })""")
                    # Sampled snapshots are an initial-draw control. A real sampled output
                    # forbids data updates and resamples on zoom; do not imply otherwise.
                    modes = ["react", "relayout"] if budget else ["react", "update", "relayout"]
                    rows: dict[str, list[dict[str, float]]] = {mode: [] for mode in modes}
                    for iteration in range(repeats + 1):
                        for mode in modes if iteration % 2 == 0 else list(reversed(modes)):
                            ms = page.evaluate(OPERATION, mode)
                            if iteration:
                                rows[mode].append(ms)
                    details["median_ms"] = {
                        mode: statistics.median(sample["promise_ms"] for sample in row)
                        for mode, row in rows.items()
                    }
                    details["median_after_two_frames_ms"] = {
                        mode: statistics.median(sample["after_two_frames_ms"] for sample in row)
                        for mode, row in rows.items()
                    }
                    details["samples_ms"] = rows
                    details["resample_budget"] = budget
                    if profiles:
                        details["profiles"] = {
                            mode: capture(
                                page,
                                lambda mode=mode, page=page: page.evaluate(OPERATION, mode),
                                profiles / f"{case}-{mode}",
                            )
                            for mode in modes
                        }
                    details["page_errors"] = errors
                    if errors:
                        raise RuntimeError(f"Browser errors in {case}: {errors}")
                    results["cases"][case] = details
                finally:
                    page.close()
        finally:
            browser.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/shiny-plotly-realworld"))
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--headless-shell", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    result = json.dumps(
        run(
            args.cache, args.cases, args.repeats, args.profiles, headless_shell=args.headless_shell
        ),
        indent=2,
    )
    if args.output:
        args.output.write_text(result + "\n")
    print(result)


if __name__ == "__main__":
    main()
