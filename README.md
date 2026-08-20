# shiny-plotly

Render [plotly](https://plotly.com/python/) figures in [Shiny for Python](https://shiny.posit.co/py/) with plain plotly.js, without the shinywidgets layer.

*An independent project, not affiliated with or endorsed by Posit or Plotly.*

[![PyPI](https://img.shields.io/pypi/v/shiny-plotly)](https://pypi.org/project/shiny-plotly/)
[![CI](https://github.com/rvben/shiny-plotly/actions/workflows/ci.yml/badge.svg)](https://github.com/rvben/shiny-plotly/actions/workflows/ci.yml)

```python
from shiny_plotly import output_plotly, render_plotly

# UI
output_plotly("sales")


# server
@render_plotly
def sales():
    return go.Figure(go.Bar(x=months, y=totals))
```

That is the whole API surface for the common case. The figure travels as plotly's own JSON over Shiny's websocket; a small output binding draws it with `Plotly.newPlot` the first time and `Plotly.react` on every re-render, into one graph div it keeps. No ipywidgets, no kernel comm, no anywidget. Every render replaces the figure, which is how most dashboards already use `@render_widget`; zoom and pan survive it when the figure sets `layout.uirevision`.

## Why

`shinywidgets` renders a plotly figure by wrapping it in a `FigureWidget` and shipping it through the ipywidgets comm protocol. That machinery earns its keep when the app mutates a figure in place (`fig.data[0].y = ...`) and wants the browser to patch it. Most Shiny apps do not do that; they rebuild the figure inside a reactive function and let Shiny re-render the output. For those apps the widget layer is overhead:

- extra dependencies (`ipywidgets`, `anywidget`, `shinywidgets`) and their JavaScript bundles on every page;
- a second rendering path next to Shiny's own, with its own quirks around sizing and full screen;
- figures held as widget state on the server for the life of the session.

`shiny-plotly` sends the figure as plotly JSON and draws it with plotly.js directly, through a Shiny output binding. The plotly.js bundle is served straight from the installed `plotly` wheel, keyed by its version, pre-compressed and with an immutable cache lifetime, so nothing is copied or vendored and a browser fetches it once.

Measured on the same app (a slider and one fillable card with a line chart; `bench/`), shiny 1.7.0, plotly 6.9.0, shinywidgets 0.8.1, shiny-plotly 0.2.0, headless Chromium, 2026-08-19:

| | shinywidgets | shiny-plotly |
| --- | --- | --- |
| Packages added on top of `shiny` + `plotly` | 24 (38 MB) | 1 (38 kB) |
| First visit, bytes to the first figure | 10.7 MB (5.3 MB HTTP + 5.4 MB websocket) | 2.6 MB (2.6 MB HTTP + 9 kB websocket) |
| of which plotly.js over HTTP | 0 (in the websocket) | 1.2 MB (brotli; 1.5 MB gzip) |
| Repeat visit (warm browser cache) | 5.4 MB, nearly all websocket | 13 kB |
| Websocket bytes per re-render | 5.4 MB | 10 kB |
| Re-render round trip, median of 50 | 1.1 to 1.4 s | 11 to 14 ms |

Both need plotly.js in the browser. shiny-plotly serves `plotly.min.js` compressed (4.9 MB raw) with `Cache-Control: immutable`, so a browser fetches it once per plotly version; shinywidgets sends plotly's widget bundle as part of the `FigureWidget` state over the websocket, and a re-render creates a new `FigureWidget`, so that cost is paid on every visit and every re-render. The round-trip numbers come from a loaded laptop and are a range across runs, not a constant. shinywidgets does things this package does not (arbitrary in-place `FigureWidget` mutation, any ipywidget), which the table does not measure; the common in-place updates, appending points and changing trace or layout attributes, are covered by `extend_traces`, `restyle` and `relayout` below. `make bench` reproduces it; `bench/results.json` holds the raw numbers.

## Install

```sh
uv add shiny-plotly
# or
pip install shiny-plotly
```

Requires Python 3.10+, `shiny>=1.0`, `plotly>=5.5`.

## Use

### Core

```python
import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny import App, ui

from shiny_plotly import output_plotly, render_plotly

app_ui = ui.page_fillable(
    ui.input_slider("n", "Points", 10, 500, 100),
    ui.card(
        ui.card_header("Fills the card; try full screen"),
        output_plotly("walk"),
        full_screen=True,
    ),
)


def server(input, output, session):
    @render_plotly
    def walk():
        rng = random.Random(input.n())
        y = list(accumulate(rng.gauss(0, 1) for _ in range(input.n())))
        return go.Figure(go.Scatter(y=y, mode="lines"))


app = App(app_ui, server)
```

Anything that is a `plotly.graph_objects.Figure` works, including what `plotly.express` builds (install `plotly[express]` for that).

### Express

```python
import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny.express import input, ui

from shiny_plotly import render_plotly

ui.page_opts(fillable=True)

with ui.sidebar():
    ui.input_slider("n", "Points", 10, 500, 100)

with ui.card(full_screen=True):

    @render_plotly
    def walk():
        rng = random.Random(input.n())
        y = list(accumulate(rng.gauss(0, 1) for _ in range(input.n())))
        return go.Figure(go.Scatter(y=y, mode="lines"))
```

The decorator creates its own output placeholder in Express, just like `@render_widget` does.

### Options

```python
@render_plotly(
    height="300px",  # fixed height; default None fills the container
    width="100%",
    figurewidget_margins=True,  # the l16/t32/r16/b16 margins shinywidgets applies
    config={"displaylogo": False},
    events=("click", "selected"),  # arrive as input.sales_click, input.sales_selected
    max_event_points=10_000,  # above it an event carries the count and range, not the points
    theme="auto",  # follow the page's color mode in the browser; also takes (light, dark)
    post_script=MORE_JS,  # JavaScript run once, when the graph is first drawn
)
def sales(): ...
```

`None` from the render function empties the output. The function may be sync or async. It may also return `fig.to_dict()` instead of a `Figure`. Anything plotly's own encoder serializes is fine as trace data: numpy arrays, pandas columns, datetimes.

### Re-renders, zoom and pan

Each `output_plotly` holds one plotly graph div. The first figure is drawn with `Plotly.newPlot`; every later one goes through `Plotly.react`, which diffs the new figure into the graph that is already there. So the DOM node, the event handlers (from `events=` or `post_script`) and plotly's per-graph state all survive a re-render.

Whether the user's zoom and pan survive is plotly's `uirevision` rule, the same one shinywidgets users rely on for in-place updates: set `layout.uirevision` to any value and keep it the same across renders to preserve the view, change it to reset the view, leave it unset to reset on every render.

```python
@render_plotly
def prices():
    return px.line(frame(), x="date", y="close").update_layout(uirevision="prices")
```

### Migrating from shinywidgets

| shinywidgets | shiny-plotly |
| --- | --- |
| `from shinywidgets import output_widget, render_widget` | `from shiny_plotly import output_plotly, render_plotly` |
| `output_widget("id")` | `output_plotly("id")` |
| `output_widget("id", height="300px")` | `output_plotly("id", height="300px")` |
| `@render_widget` | `@render_plotly` |
| (FigureWidget margins, applied implicitly) | `@render_plotly(figurewidget_margins=True)` |

Three things change on purpose:

- **Margins.** shinywidgets sets tight margins (`l=16, t=32, r=16, b=16`) on every FigureWidget; plotly's own defaults are `80/100/80/80`. `shiny-plotly` uses plotly's defaults unless you pass `figurewidget_margins=True`, which fills in only the sides your figure leaves unset. Set margins explicitly on the figure if you want something else.
- **In-place mutation.** A `FigureWidget` you keep on the server and mutate (`fig.data[0].y = ...`, `fig.add_trace(...)` after render) is exactly what shinywidgets is for. `shiny-plotly` has no channel for that; return a new figure from the render function and let Shiny re-render. If your app depends on in-place widget updates, stay on shinywidgets for those outputs. Both packages can coexist in one app.
- **Zoom across re-renders.** A mutated `FigureWidget` keeps the user's zoom because nothing replaces the figure. Here a re-render is a new figure, so plotly's `uirevision` decides: set `layout.uirevision` (see above) to keep the view.

### Sizing

The rules mirror `output_widget`:

- `height=None` (default): the plot fills its container. Inside `ui.card(full_screen=True)`, a fillable page or a sidebar layout it grows and shrinks with the card, from a 400px basis. Outside a fill layout it is 400px tall.
- `height="300px"` (on the decorator or on `output_plotly`): the plot is exactly that tall and opts out of filling.

Plotly alone re-measures a graph only on window resize. `shiny-plotly` ships a small helper script (`shiny-plotly.js`, loaded with every output) that observes each graph's container with a `ResizeObserver`, so a card that changes size without a window resize, for example when a sibling output renders below it, or when a sidebar collapses, re-lays the graph out. The same helper purges a graph once it leaves the document, which releases the window listener and layout state plotly would otherwise keep.

### Dark mode

Plotly does not follow Bootstrap's color mode by itself. `theme="auto"` makes the figure follow it in the browser, with no server round-trip:

```python
app_ui = ui.page_fillable(
    ui.input_dark_mode(),
    ui.card(output_plotly("sales")),
)


@render_plotly(theme="auto")
def sales():
    return px.bar(df, x="month", y="total")
```

`"auto"` pairs plotly's own templates: `"plotly"` in light mode, `"plotly_dark"` in dark. A `(light, dark)` tuple picks different ones, each a registered name, a plotly `Template` object or a template dict:

```python
@render_plotly(theme=("seaborn", "plotly_dark"))
def sales(): ...
```

How it works: both templates travel with the figure, their `paper_bgcolor` and `plot_bgcolor` made transparent so the card's own background shows through in both modes (backgrounds set on the figure's layout still win). The browser applies the mode's template before the first draw and switches it with `Plotly.relayout` when the mode flips, so the switch is instant and works even while the server is busy. The mode is `data-bs-theme` on `<html>`, which is what `ui.input_dark_mode()` maintains, or the OS `prefers-color-scheme` on a page without that attribute. A template the figure baked in through `layout.template` is dropped for themed outputs; use `theme=None` (the default) where the figure's own template should stand.

The manual alternative, picking the template on the server, still works and is the way to vary anything beyond the template per mode. Give the dark mode switch an id and read it in the render function; flipping the switch then re-renders the figure through `Plotly.react`:

```python
app_ui = ui.page_fillable(
    ui.input_dark_mode(id="mode"),
    ui.card(output_plotly("sales")),
)


@render_plotly
def sales():
    template = "plotly_dark" if input.mode() == "dark" else "plotly"
    fig = px.bar(df, x="month", y="total", template=template)
    return fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
```

### Events back to Shiny

`events=` names the plotly events to forward; each arrives as `input.<id>_<event>`, namespaced like the output inside a module. Seven are available: `click`, `doubleclick`, `hover`, `selected`, `relayout`, `legendclick` and `legenddoubleclick`.

```python
@render_plotly(events=("click", "selected"))
def scatter(): ...


@render.text
def click_info():
    if not input.scatter_click.is_set():
        return "Click a point."
    pt = input.scatter_click()["points"][0]
    return f"trace {pt['curveNumber']}, point {pt['pointNumber']}: x={pt['x']}, y={pt['y']}"
```

What arrives is plotly's own event data, cut to what serializes, the same way Dash cuts it:

| event | value of `input.<id>_<event>()` |
| --- | --- |
| `click` | `{"points": [...]}`; fires on every click, a repeated one too |
| `hover` | `{"points": [...]}` while over a point, `None` once the pointer leaves; debounced (100 ms) |
| `selected` | `{"points": [...], "range": {"x": [..], "y": [..]}}` for a box, `lassoPoints` for a lasso; `None` after a double-click deselect; above `max_event_points` the points give way to `point_count` (below) |
| `relayout` | plotly's relayout data as is: `{"xaxis.range[0]": ..., "xaxis.range[1]": ...}` after a zoom or pan, `{"xaxis.autorange": True, ...}` after a reset, `{"dragmode": "pan"}` from the mode bar, `{"autosize": True}` after a resize |
| `doubleclick` | a running count of double-clicks on the plot area (the gesture that resets the axes); plotly hands the event no data, and the count's change is what invalidates the input |
| `legendclick` | `{"curve_number": 1, "expanded_index": 1, "name": "beta", "visible": True}` for the trace whose legend item was clicked, `visible` as it stood before the click's toggle (`True` or `"legendonly"`); trace types whose legend items are labels (pie, funnelarea) add `label`; fires on every click, and the default toggle still happens |
| `legenddoubleclick` | same value as `legendclick`; the default isolate-this-trace behavior still happens |

Each point carries plotly's scalar fields for that trace type (`curveNumber`, `pointNumber`, `pointIndex`, `x`, `y`, `z`, `text`, `label`, `value`, `lat`, `lon`, ...) plus `customdata` (as a plain list, also when it was a numpy array), `bbox` and `pointNumbers` when present. `input.<id>_<event>()` raises a silent exception until the event has fired once, so check `is_set()` when the output should show something before that.

#### Dense traces

A point is about 100 bytes of JSON, so a box over a dense trace builds a large message, and a large enough one ends the session: uvicorn closes a websocket on a message above 16 MB by default. `max_event_points` (default 10 000) is the most points one event carries. Above it the points stay in the browser and the value says so, with the selection's geometry intact:

```python
@render_plotly(events="selected")
def scatter(): ...


@render.text
def picked():
    sel = input.scatter_selected()
    if sel is None:
        return "Nothing selected."
    if sel["points"] is not None:
        return f"{len(sel['points'])} points"
    # More than max_event_points: {"points": None, "point_count": 120000, "range": {...}}.
    # The data is here, so membership is a filter on the box the user dragged.
    (x0, x1), (y0, y1) = sel["range"]["x"], sel["range"]["y"]
    inside = df[df.x.between(x0, x1) & df.y.between(y0, y1)]
    return f"{sel['point_count']} points, {len(inside)} rows"
```

The value is never silently cut: `points` is a full list or `None`, and `point_count` is there when it is `None`. A lasso carries `lassoPoints` (the polygon's `x` and `y` lists) instead of `range`. `max_event_points=None` lifts the cap. Measured with `make bench-events` (one `Scattergl` trace, every point box-selected with a real mouse, headless Chromium and the server on the same laptop, 2026-08-19):

| points | `max_event_points` | event JSON | mouse up to server |
| --- | --- | --- | --- |
| 1 000 | 10 000 | 99 kB | 83 ms |
| 10 000 | 10 000 | 1.01 MB | 149 ms |
| 100 000 | 10 000 | 136 B | 24 ms |
| 100 000 | none | 10.33 MB | 1017 ms |
| 200 000 | 10 000 | 135 B | 108 ms |
| 200 000 | none | 20.89 MB | disconnected |

`click` and `hover` carry one point per trace under the pointer, so the cap matters for `selected`; hover is also debounced (100 ms), so a pointer sweeping across a dense trace sends one event when it rests, not one per point.

For anything else, `post_script` runs once, after the first figure is drawn, with `{plot_id}` replaced by the graph div's id. Re-renders go through `Plotly.react` into the same graph div, so handlers attached either way stay attached and are never stacked.

```python
ANNOTATION_TO_INPUT = """
document.getElementById('{plot_id}').on('plotly_clickannotation', function (ev) {
    Shiny.setInputValue('annotation', ev.index, {priority: 'event'});
});
"""


@render_plotly(post_script=ANNOTATION_TO_INPUT)
def scatter(): ...
```

### Live updates without a re-render

A re-render sends the whole figure. For a stream of points, a colour change or a new title, send just the change: `extend_traces`, `prepend_traces`, `add_traces`, `delete_traces`, `restyle`, `relayout` and `update` call the plotly.js functions of the same names on the graph an output holds. All of them are coroutines, so the effect that calls them is `async def`.

```python
from shiny_plotly import extend_traces, relayout, restyle


@render_plotly
def prices():
    return go.Figure(go.Scatter(x=[], y=[], mode="lines"))  # the seed; the stream fills it


@reactive.effect
async def _stream():
    reactive.invalidate_later(1)
    t, v = latest_sample()
    await extend_traces("prices", {"x": [[t]], "y": [[v]]}, max_points=500)


@reactive.effect
@reactive.event(input.highlight)
async def _highlight():
    await restyle("prices", {"line.color": "crimson"}, indices=0)
    await relayout("prices", {"title.text": "highlighted"})
```

- `extend_traces(id, data, indices=None, *, max_points=None)`: `data` maps an array attribute to one sequence of new values per trace, in the order of `indices` (`{"x": [[t]], "y": [[v]]}` appends one point to one trace; `{"y": [[1], [2]]}` with `indices=[0, 1]` one point to each of two). `indices` (an int or a list) defaults to every trace; `max_points` drops the oldest points past that many, for a rolling window.
- `restyle(id, update, indices=None)`: `update` maps attribute paths to values; `{"marker.color": "red"}` applies to every trace in `indices`, a list value applies per trace (`{"opacity": [0.5, 1]}` with `indices=[0, 1]`).
- `relayout(id, update)`: layout attribute paths, `{"title.text": "Live"}`, `{"xaxis.range": [0, 10]}`, `{"xaxis.autorange": True}`. With `events="relayout"` on the output, the result comes back as `input.<id>_relayout`, the same as a user's zoom.
- `update(id, restyle=None, relayout=None, indices=None)`: a restyle and a relayout in one redraw, where separate calls would draw twice. At least one of the two is required; `indices` scopes the trace part.
- `prepend_traces(id, data, indices=None, *, max_points=None)`: the mirror of `extend_traces`; the new values go in front, and `max_points` drops points from the far end.
- `add_traces(id, traces, indices=None)`: whole new traces, each a graph object (`go.Scatter(y=[1, 2])`) or a dict (`{"y": [1], "type": "bar"}`); `indices` says where they land in the trace order, appended without it.
- `delete_traces(id, indices)`: removes the traces at `indices`; the rest renumber, as a Python list does on `del`.

The values go through plotly's encoder, so numpy arrays, pandas columns and datetimes work. The id is namespaced inside a module, like the output. An update reaches the figure that is drawn at that moment; one sent while the output has no figure (its first render is still running, it sits in a hidden tab, it shows an error or was emptied by `None`) is held and applied, in order, right after the output's next draw. A re-render replaces the figure, updates included, with what the render function returns: the server stays the source of truth, and a figure that should keep its streamed points across a re-render builds them in from server-side state.

A runnable version of the streaming pattern, with a pause switch and a window slider, is `examples/streaming_app.py`.

### Lower level

- `fig_to_ui(fig, div_id=None, *, height, width, figurewidget_margins, config, post_script)` returns a `TagList` holding the plotly.js dependency, the helper dependency and a `<div class="shiny-plotly">` that draws the figure with `Plotly.newPlot` (plotly's own `to_html` fragment). Use it from a plain `@render.ui` that composes a figure with other UI, or from any htmltools context. Each render draws a fresh graph; an output that is only a figure is better served by `render_plotly`.
- `plotly_js()` is the `HTMLDependency` for plotly.js, served from the installed `plotly` wheel at `/lib/plotly-<version>/plotly.min.js`. Every `output_plotly` and every `fig_to_ui` fragment carries it, so it is optional; add it to the page UI when the first figure is inserted later (`ui.insert_ui`, a `@render.ui` that starts empty) and the bundle should load with the page.
- `shiny_plotly_js()` is the helper's dependency. Every output and fragment carries it too.
- `FIGUREWIDGET_MARGINS` is the `{"l": 16, "t": 32, "r": 16, "b": 16}` mapping.
- `enable_compressed_plotly_js(app)` turns on compressed, immutable serving of plotly.js for a `shiny.App` before its first session (see below).
- `extend_traces`, `restyle` and `relayout` take an optional `session=` when called outside the current session's context.

`render_plotly` needs `output_plotly`; it is an output binding, not a `render.ui`, so `ui.output_ui(id)` does not draw it.

### plotly.js on the wire

Shiny serves HTML dependencies from a plain static mount: no compression, no `Cache-Control`. `plotly.min.js` is 4.9 MB, so once the first session of a process has rendered a figure, `shiny-plotly` adds a route in front of that mount for the bundle's exact path (`/lib/plotly-<version>/plotly.min.js`) that serves it pre-compressed (brotli when the `brotli` package is installed, gzip otherwise; 1.2 MB or 1.5 MB on the wire) with `Cache-Control: public, max-age=31536000, immutable`, `Vary: Accept-Encoding` and an `ETag` per encoding. The URL is keyed by the plotly version, so a browser fetches each version once. Compression runs once per process, in a background thread; until it has finished the route serves the raw file with the same headers.

```sh
uv add "shiny-plotly[brotli]"  # optional: brotli instead of gzip
```

Two things to know. The page load that starts the very first session of a process has already asked for the bundle before the route exists, so that one visitor gets the raw file from Shiny's mount; everyone after gets the compressed one. A Core app can close that gap by enabling the route as soon as the `App` exists:

```python
from shiny_plotly import enable_compressed_plotly_js

app = App(app_ui, server)
enable_compressed_plotly_js(app)
```

And if a reverse proxy in front of the app does its own compression and caching, or you want Shiny's static serving untouched for any reason, set `SHINY_PLOTLY_NO_COMPRESS=1` in the app's environment; `enable_compressed_plotly_js` then returns `False` and adds nothing.

### Shinylive

Apps using `shiny-plotly` run under [Shinylive](https://shiny.posit.co/py/get-started/shinylive.html) (pyodide in the browser) as well; list `shiny-plotly` in the app's `requirements.txt` next to `plotly`. There is no HTTP server in the browser and pyodide cannot start threads, so the compression route above stands down under pyodide (`enable_compressed_plotly_js` returns `False`); everything else, rendering, events, in-place updates and themes, is browser-side already. Verified against a real `shinylive export`; `examples/shinylive/` is a ready-to-export app.

## Examples

```sh
uv run --with shiny-plotly shiny run examples/core_app.py     # fill, margins, a click input, a live stream
uv run --with shiny-plotly shiny run examples/express_app.py  # the Express flavor
uv run --with shiny-plotly shiny run examples/dark_app.py     # theme="auto" and a custom (light, dark) pair
uv run --with shiny-plotly shiny run examples/events_app.py   # box selections over a 50k-point trace
uv run --with shiny-plotly shiny run examples/streaming_app.py # a rolling window fed by extend_traces
uvx shinylive export examples/shinylive site                  # the same package, running in the browser
```

## Development

```sh
make sync        # uv sync --all-groups
make browsers    # playwright install chromium, once
make check       # lint, typecheck, unit + e2e tests, browser tests, wheel check, floor check
make bench       # the shinywidgets comparison above, on this machine
make bench-events  # what a selection over a dense trace costs, capped and uncapped
```

`make test` runs the unit tests and the in-process Shiny end-to-end tests over a real websocket, including the compressed bundle route. `make test-browser` drives the package in headless Chromium: fill sizing, resize without a window event, the graph div surviving a re-render, `uirevision` keeping a dragged zoom, purge once an output leaves the page, full screen, `events=` click, hover, selection and relayout inputs (attached once, also inside a module, a selection above `max_event_points` arriving as count and range), `extend_traces`, `restyle` and `relayout` applied in place (rolling window, one trace or all, held until the first draw, reset by a re-render, inside a module, dropped with a warning for an unknown output), `post_script` click wiring (once, not stacked), the dark mode recipe, error and `None` rendering, on-demand loading of plotly.js and the compressed, cached bundle as a fresh visitor sees it. `make check-wheel` installs the built wheel into a throwaway venv and runs the suite against it, so the published artifact is what was tested. `make check-floor` installs the package with plotly, shiny and htmltools at the oldest versions `pyproject.toml` allows and runs the whole suite again, browser tests included, so the declared lower bounds are tested on every push rather than assumed.

## License

MIT. See [LICENSE](LICENSE).
