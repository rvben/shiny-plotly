"""Prove an exported shinylive demo renders its figures in a real browser.

With --site, serves that directory at --url (the URL the site was built for) and drives
headless Chromium through both tabs: pyodide boots, micropip installs the wheel, the
stream accumulates points, the Explore tab draws. Any console error or uncaught page
error fails the check, "can't start new thread" (the pyodide crash 0.3.1 fixed)
included. Without --site the probe drives an already-deployed --url, e.g. the live
Pages site after a deploy.

    uv run python tools/site_check.py --site site --url http://127.0.0.1:8008
    uv run python tools/site_check.py --url https://rvben.github.io/shiny-plotly/
"""

from __future__ import annotations

import argparse
import functools
import http.server
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Frame, Page, sync_playwright

BOOT_TIMEOUT_SECONDS = 180
STREAM = "document.querySelector('#stream .plotly-graph-div')"


def serve(site: Path, url: str) -> http.server.ThreadingHTTPServer:
    parsed = urlparse(url)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    address = (parsed.hostname or "127.0.0.1", parsed.port or 80)
    server = http.server.ThreadingHTTPServer(address, handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def app_frame(page: Page) -> Frame | None:
    """The iframe shinylive mounts the app in, once it exists."""
    return next((f for f in page.frames if f is not page.main_frame), None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the URL the site was built for")
    parser.add_argument("--site", type=Path, help="exported site directory to serve at --url")
    args = parser.parse_args()

    server = serve(args.site, args.url) if args.site else None
    started = time.monotonic()
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.goto(args.url)

            # Pyodide boot, wheel install, app start and the first draw all sit behind
            # this one selector, inside the iframe shinylive mounts the app in; the
            # first error aborts the wait instead of timing out.
            deadline = started + BOOT_TIMEOUT_SECONDS
            frame: Frame | None = None
            while True:
                if errors:
                    break
                frame = app_frame(page)
                if frame and frame.locator("#stream svg.main-svg").first.is_visible():
                    break
                if time.monotonic() > deadline:
                    print(f"no figure after {BOOT_TIMEOUT_SECONDS}s", file=sys.stderr)
                    return 1
                page.wait_for_timeout(500)

            if not errors and frame:
                # Ticks flow: extend_traces messages reach the graph div under pyodide.
                frame.wait_for_function(
                    f"() => {{ const gd = {STREAM}; "
                    "return gd && gd.data && gd.data[0].x.length >= 2; }"
                )
                frame.get_by_role("tab", name="Explore").click()
                frame.locator("#explore svg.main-svg").first.wait_for(state="visible")
            browser.close()
    finally:
        if server:
            server.shutdown()

    if errors:
        print("the page raised errors:", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"rendered both tabs in {time.monotonic() - started:.0f}s at {args.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
