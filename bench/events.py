"""Measure what a ``selected`` event costs on a dense trace, browser to server.

Run with ``make bench-events`` (``BENCH_ARGS="--points 1000 10000 100000"`` to choose the
sizes); it needs the dev dependency group and playwright's Chromium. For each size it
draws one scatter trace of that many points, box-selects every one of them with a real
mouse, and reports the JSON size of the event, the time from the mouse going up until the
server has rendered an output reading it, and whether the session survived; once with the
points capped by ``max_event_points`` at its default and once uncapped.

This is the measurement behind the ``max_event_points`` option: uncapped, a selection of
every point of a 200k-point trace is a 20 MB websocket message, above the 16 MB limit
uvicorn applies by default, and the connection closes with the session. The uncapped
column past that size is therefore expected to read "disconnected" rather than a time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass

import numpy as np
import plotly.graph_objects as go
import uvicorn
from playwright.sync_api import Browser, sync_playwright
from shiny import App, Inputs, Outputs, Session, render, ui
from starlette.applications import Starlette
from starlette.routing import Mount

from shiny_plotly import DEFAULT_MAX_EVENT_POINTS, output_plotly, render_plotly

HOST = "127.0.0.1"
SIZES = (1_000, 10_000, 100_000, 200_000)


def make_app(points: int, max_event_points: int | None) -> App:
    app_ui = ui.page_fluid(
        output_plotly("sel", height="400px", width="600px"),
        ui.output_text("received"),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(events="selected", max_event_points=max_event_points)
        def sel():
            rng = np.random.default_rng(0)
            trace = go.Scattergl(x=rng.random(points), y=rng.random(points), mode="markers")
            margin = dict(l=40, r=10, t=10, b=40)
            return go.Figure(trace).update_layout(dragmode="select", margin=margin)

        @render.text
        def received():
            # The server-side timestamp, so the browser can compute the round trip.
            value = input.sel_selected()
            if value is None:
                return "none"
            count = value["point_count"] if value["points"] is None else len(value["points"])
            return f"{count} points at {time.time():.3f}"

    return App(app_ui, server)


@dataclass
class Measurement:
    points: int
    max_event_points: int | None
    payload_bytes: int
    stringify_ms: float
    round_trip_ms: int | None  # mouse up to the server-rendered output; None: disconnected
    disconnected: bool


OVERLAY_JS = "document.getElementById('shiny-disconnected-overlay') !== null"

MEASURE_JS = """
() => {
  const original = window.Shiny.setInputValue;
  window.measure = null;
  window.mouseUpAt = null;
  document.addEventListener("mouseup", () => { window.mouseUpAt = Date.now() / 1000; }, true);
  window.Shiny.setInputValue = function (name, value, options) {
    const t0 = performance.now();
    const bytes = JSON.stringify(value).length;
    window.measure = { bytes, stringify_ms: performance.now() - t0, sent_at: Date.now() / 1000 };
    return original.call(this, name, value, options);
  };
}
"""


def measure(browser: Browser, points: int, max_event_points: int | None) -> Measurement:
    root = Starlette(routes=[Mount("/", app=make_app(points, max_event_points))])
    server = uvicorn.Server(uvicorn.Config(root, host=HOST, port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        page = browser.new_page()
        page.goto(f"http://{HOST}:{port}/")
        # Set by the helper right after the first draw, once the event handlers are on.
        page.wait_for_function(
            "() => (document.getElementById('sel-plotly') || {})._shinyPlotlyDrawn === true"
        )
        page.evaluate(MEASURE_JS)
        box = page.locator("#sel .nsewdrag").bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + 1, box["y"] + 1)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 1, box["y"] + box["height"] - 1, steps=4)
        page.mouse.up()
        disconnected = False
        try:
            page.wait_for_function(
                "() => document.getElementById('received').textContent.includes(' at ') || "
                + OVERLAY_JS,
                timeout=60_000,
            )
            disconnected = page.evaluate(OVERLAY_JS)
        except Exception:  # playwright's TimeoutError: neither an answer nor a closed session
            disconnected = True
        sent = page.evaluate("window.measure")
        round_trip = None
        if not disconnected:
            # Browser and server share this machine's clock.
            server_at = float(page.locator("#received").inner_text().split(" at ")[1])
            round_trip = round((server_at - page.evaluate("window.mouseUpAt")) * 1000)
        page.close()
        return Measurement(
            points=points,
            max_event_points=max_event_points,
            payload_bytes=sent["bytes"],
            stringify_ms=round(sent["stringify_ms"], 1),
            round_trip_ms=round_trip,
            disconnected=disconnected,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def report(rows: list[Measurement]) -> str:
    lines = [
        "| points | max_event_points | event JSON | mouse up to server |",
        "| --- | --- | --- | --- |",
    ]
    for m in rows:
        cap = "none" if m.max_event_points is None else f"{m.max_event_points:,}"
        if m.payload_bytes >= 100_000:
            size = f"{m.payload_bytes / 1e6:.2f} MB"
        elif m.payload_bytes >= 1_000:
            size = f"{m.payload_bytes / 1e3:.0f} kB"
        else:
            size = f"{m.payload_bytes} B"
        trip = "disconnected" if m.disconnected else f"{m.round_trip_ms} ms"
        lines.append(f"| {m.points:,} | {cap} | {size} | {trip} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--points", type=int, nargs="+", default=list(SIZES), help="trace sizes to measure"
    )
    parser.add_argument("--json", action="store_true", help="raw measurements as JSON")
    args = parser.parse_args(argv)
    rows: list[Measurement] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for points in args.points:
            for cap in (DEFAULT_MAX_EVENT_POINTS, None):
                row = measure(browser, points, cap)
                rows.append(row)
                print(f"{points:,} points, cap {cap}: {asdict(row)}", file=sys.stderr)
        browser.close()
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
    else:
        print(report(rows))
        print(f"\nload average at the end of the run: {os.getloadavg()[0]:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
