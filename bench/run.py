"""Measure shinywidgets against shiny-plotly on identical apps (see ``bench/common.py``).

Run with ``make bench``; it needs the ``bench`` dependency group and playwright's Chromium.
Writes a Markdown report to stdout and the raw numbers to ``bench/results.json``.

What is measured, and why these:

- Install footprint: packages and bytes each stack adds on top of ``shiny`` + ``plotly``,
  and the time to import it. Deterministic.
- Page load: bytes over HTTP and over the Shiny websocket until the first figure is on
  screen, and the request count; once on a first visit and once more on a repeat visit
  in the same browser context, where static files come from the browser cache and only
  what travels over the websocket is paid again. Deterministic up to compression. Each
  server gets one warm-up visit first, from a throwaway browser context, so what is
  measured is what every visitor after the first sees: shiny-plotly installs its
  compressed, immutable route for plotly.min.js when the first session starts, and the
  page load that started that session has already fetched the bundle from Shiny's own
  static mount by then.
- Re-render round trip: slider change to the new trace in the DOM, reported as the
  median and p90 of N runs. The least stable number; the load average at the time of
  the run is recorded next to it.

Server memory is deliberately not measured: the RSS of a process is not a stable number
on a machine whose memory compressor is active (it went down during trial runs), and a
number that cannot be reproduced is worse than none.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, WebSocket, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PLOT_READY = (
    "n => { const gd = document.querySelector('.js-plotly-plot');"
    " return !!(gd && gd.data && gd.data[0] && gd.data[0].x.length === n); }"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Server:
    """One of the bench apps, as its own uvicorn process on a free port."""

    def __init__(self, app: str) -> None:
        self.port = free_port()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                app,
                "--host",
                HOST,
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((HOST, self.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.stop()
            raise RuntimeError(f"{app} did not start listening on {self.port}")
        self.url = f"http://{HOST}:{self.port}/"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@dataclass
class Traffic:
    http_bytes: int = 0
    http_requests: int = 0
    http_requests_on_wire: int = 0
    plotly_js_bytes: int = 0
    ws_received: int = 0
    ws_sent: int = 0


# What the page fetched over HTTP, from the browser's own Resource Timing: transferSize is
# headers plus the encoded body as they crossed the wire, and 0 for a cache hit. (Playwright's
# request.sizes() reports header bytes for cache hits too, which overstates a warm visit.)
HTTP_TRAFFIC = """() => {
  const entries = [
    ...performance.getEntriesByType('navigation'),
    ...performance.getEntriesByType('resource'),
  ];
  const plotly = entries.find(e => e.name.split('?')[0].endsWith('/plotly.min.js'));
  return {
    http_bytes: entries.reduce((n, e) => n + e.transferSize, 0),
    http_requests: entries.length,
    http_requests_on_wire: entries.filter(e => e.transferSize > 0).length,
    plotly_js_bytes: plotly ? plotly.transferSize : 0,
  };
}"""


class Meter:
    """Counts every byte a page exchanges over websockets."""

    def __init__(self, page: Page) -> None:
        self.traffic = Traffic()
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, ws: WebSocket) -> None:
        ws.on("framereceived", lambda payload: self._count("ws_received", payload))
        ws.on("framesent", lambda payload: self._count("ws_sent", payload))

    def _count(self, attr: str, payload: str | bytes) -> None:
        n = len(payload) if isinstance(payload, bytes) else len(payload.encode())
        setattr(self.traffic, attr, getattr(self.traffic, attr) + n)

    def snapshot(self) -> Traffic:
        return Traffic(**asdict(self.traffic))


def diff(after: Traffic, before: Traffic) -> Traffic:
    return Traffic(**{k: getattr(after, k) - getattr(before, k) for k in asdict(after)})


@dataclass
class AppResult:
    name: str
    page_load: dict[str, Any] = field(default_factory=dict)
    repeat_visit: dict[str, Any] = field(default_factory=dict)
    rerender: dict[str, Any] = field(default_factory=dict)


def load_page(context: BrowserContext, url: str) -> tuple[Page, Meter, dict[str, Any]]:
    """Opens the app in a new page; returns it, its meter, and what showing the figure cost.

    The meter is returned because it must be attached before the websocket opens; one
    attached later never sees that socket's frames.
    """
    page = context.new_page()
    meter = Meter(page)
    t0 = time.perf_counter()
    page.goto(url)
    page.wait_for_function(PLOT_READY, arg=100, timeout=30_000)
    first_plot = time.perf_counter() - t0
    page.wait_for_timeout(500)
    traffic = {**asdict(meter.snapshot()), **page.evaluate(HTTP_TRAFFIC)}
    return page, meter, {**traffic, "time_to_first_plot_s": first_plot}


def warm_up(browser: Browser, url: str) -> None:
    """One visit from a throwaway context, then wait for any background work it started.

    The wait is on the plotly bundle being served compressed, which is the visible end of
    shiny-plotly's one-off compression; an app that never serves the bundle over HTTP
    (shinywidgets ships it over the websocket) returns at once.
    """
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(url)
        page.wait_for_function(PLOT_READY, arg=100, timeout=30_000)
        bundle = f"{url}lib/plotly-{version('plotly')}/plotly.min.js"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            resp = context.request.get(bundle, headers={"Accept-Encoding": "gzip, br"})
            if resp.status != 200 or "content-encoding" in resp.headers:
                return
            time.sleep(0.2)
    finally:
        context.close()


def measure_app(name: str, app: str, browser: Browser, renders: int) -> AppResult:
    result = AppResult(name)
    server = Server(app)
    try:
        warm_up(browser, server.url)
        context = browser.new_context()
        page, meter, result.page_load = load_page(context, server.url)
        # Same browser context, so the static files are cached; this page is closed again
        # so the re-render loop below runs in one session.
        repeat, _, result.repeat_visit = load_page(context, server.url)
        repeat.close()

        latencies: list[float] = []
        ws_per_render: list[int] = []
        for i in range(renders):
            n = 200 + (i % 2) * 100 + i  # always differs from the previous value
            before = meter.snapshot()
            t0 = time.perf_counter()
            page.evaluate("n => Shiny.setInputValue('n', n, {priority: 'event'})", n)
            page.wait_for_function(PLOT_READY, arg=n, timeout=30_000)
            latencies.append(time.perf_counter() - t0)
            page.wait_for_timeout(50)
            d = diff(meter.snapshot(), before)
            ws_per_render.append(d.ws_received + d.ws_sent)
        result.rerender = {
            "runs": renders,
            "median_s": statistics.median(latencies),
            "p90_s": statistics.quantiles(latencies, n=10)[8] if renders >= 10 else max(latencies),
            "ws_bytes_median": statistics.median(ws_per_render),
        }
        context.close()
    finally:
        server.stop()
    return result


def measure_footprint(tmp: Path) -> dict[str, Any]:
    """Packages, bytes and import time of each stack in a throwaway venv."""
    stacks = {
        "base": ["shiny", "plotly"],
        "shinywidgets": ["shiny", "plotly", "shinywidgets", "anywidget"],
        "shiny-plotly": ["shiny", "plotly", str(ROOT)],
    }
    imports = {"shinywidgets": "shinywidgets", "shiny-plotly": "shiny_plotly"}
    out: dict[str, Any] = {}
    for stack, specs in stacks.items():
        venv = tmp / stack
        subprocess.run(["uv", "venv", "--quiet", str(venv)], check=True)
        py = str(venv / "bin" / "python")
        subprocess.run(["uv", "pip", "install", "--quiet", "--python", py, *specs], check=True)
        listing = json.loads(
            subprocess.check_output(
                ["uv", "pip", "list", "--python", py, "--format", "json"],
                stderr=subprocess.DEVNULL,
            )
        )
        site = next((venv / "lib").glob("python*/site-packages"))
        size = sum(p.stat().st_size for p in site.rglob("*") if p.is_file())
        entry: dict[str, Any] = {"packages": len(listing), "site_packages_bytes": size}
        if stack in imports:
            times = []
            for _ in range(5):
                t = subprocess.check_output(
                    [
                        py,
                        "-c",
                        "import time; t=time.perf_counter(); "
                        f"import {imports[stack]}; print(time.perf_counter()-t)",
                    ],
                    text=True,
                )
                times.append(float(t))
            entry["import_s_median"] = statistics.median(times)
        out[stack] = entry
    return out


def environment(browser: Browser) -> dict[str, Any]:
    return {
        "date": date.today().isoformat(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "chromium": browser.version,
        "load_average_1m": os.getloadavg()[0],
        "versions": {
            p: version(p)
            for p in ("shiny", "plotly", "shinywidgets", "anywidget", "ipywidgets", "shiny-plotly")
        },
    }


def fmt_bytes(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f} kB"
    return f"{n:.0f} B"


def fmt_ms(s: float) -> str:
    return f"{s * 1000:.0f} ms"


def report(env: dict[str, Any], footprint: dict[str, Any], apps: list[AppResult]) -> str:
    v = env["versions"]
    w, p = apps
    base = footprint["base"]

    def added(stack: str) -> str:
        fp = footprint[stack]
        pkgs = fp["packages"] - base["packages"]
        size = fp["site_packages_bytes"] - base["site_packages_bytes"]
        return f"{pkgs} ({fmt_bytes(size)})"

    def wire(load: dict[str, Any]) -> str:
        http, ws = load["http_bytes"], load["ws_received"] + load["ws_sent"]
        return f"{fmt_bytes(http + ws)} ({fmt_bytes(http)} HTTP + {fmt_bytes(ws)} websocket)"

    def rtt(r: AppResult) -> str:
        return f"{fmt_ms(r.rerender['median_s'])} / {fmt_ms(r.rerender['p90_s'])}"

    rows = [
        ("Packages added on top of shiny + plotly", added("shinywidgets"), added("shiny-plotly")),
        (
            "Import time",
            fmt_ms(footprint["shinywidgets"]["import_s_median"]),
            fmt_ms(footprint["shiny-plotly"]["import_s_median"]),
        ),
        ("First visit, bytes to the first figure", wire(w.page_load), wire(p.page_load)),
        (
            "First visit, HTTP requests",
            str(w.page_load["http_requests"]),
            str(p.page_load["http_requests"]),
        ),
        (
            "First visit, plotly.js over HTTP",
            fmt_bytes(w.page_load["plotly_js_bytes"]),
            fmt_bytes(p.page_load["plotly_js_bytes"]),
        ),
        (
            "First visit, time to the first figure",
            fmt_ms(w.page_load["time_to_first_plot_s"]),
            fmt_ms(p.page_load["time_to_first_plot_s"]),
        ),
        (
            "Repeat visit (warm browser cache), bytes to the first figure",
            wire(w.repeat_visit),
            wire(p.repeat_visit),
        ),
        (
            "Repeat visit, HTTP requests that left the cache",
            str(w.repeat_visit["http_requests_on_wire"]),
            str(p.repeat_visit["http_requests_on_wire"]),
        ),
        (
            "Websocket bytes per re-render (median)",
            fmt_bytes(w.rerender["ws_bytes_median"]),
            fmt_bytes(p.rerender["ws_bytes_median"]),
        ),
        (f"Re-render round trip, median / p90 of {w.rerender['runs']}", rtt(w), rtt(p)),
    ]
    lines = [
        "# shinywidgets vs shiny-plotly",
        "",
        f"shiny {v['shiny']}, plotly {v['plotly']}, shinywidgets {v['shinywidgets']} "
        f"(anywidget {v['anywidget']}, ipywidgets {v['ipywidgets']}), "
        f"shiny-plotly {v['shiny-plotly']}; Python {env['python']}, {env['platform']}, "
        f"Chromium {env['chromium']}; {env['date']}, 1-minute load average "
        f"{env['load_average_1m']:.1f}.",
        "",
        "| | shinywidgets | shiny-plotly |",
        "| --- | --- | --- |",
        *(f"| {label} | {a} | {b} |" for label, a, b in rows),
        "",
        "Both stacks need plotly.js in the browser. shiny-plotly serves plotly.min.js "
        f"compressed ({fmt_bytes(p.page_load['plotly_js_bytes'])} on the wire) with a "
        "year-long immutable cache lifetime, so a repeat visit does not ask for it again; "
        "shinywidgets (plotly's FigureWidget is an anywidget) sends plotly's widget bundle as "
        "part of the widget state over the websocket, for every new FigureWidget, which is "
        "why its repeat-visit and per-re-render bytes stay where they are. shinywidgets also "
        "does things this package does not (in-place FigureWidget updates, any ipywidget), "
        "which is not measured here.",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--renders", type=int, default=50, help="re-renders per app (default 50)")
    ap.add_argument(
        "--skip-footprint", action="store_true", help="skip the venv-based install measurement"
    )
    ap.add_argument("--json", type=Path, default=ROOT / "bench" / "results.json")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="shiny-plotly-bench-"))
    try:
        footprint = {} if args.skip_footprint else measure_footprint(tmp)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            env = environment(browser)
            apps = [
                measure_app("shinywidgets", "bench.app_widgets:app", browser, args.renders),
                measure_app("shiny-plotly", "bench.app_plotly:app", browser, args.renders),
            ]
            browser.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    args.json.write_text(
        json.dumps(
            {"environment": env, "footprint": footprint, "apps": [asdict(a) for a in apps]},
            indent=2,
        )
        + "\n"
    )
    if footprint:
        print(report(env, footprint, apps))
    else:
        print(json.dumps({"environment": env, "apps": [asdict(a) for a in apps]}, indent=2))


if __name__ == "__main__":
    main()
