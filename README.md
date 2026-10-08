# shiny-plotly

Render [plotly](https://plotly.com/python/) figures in [Shiny for Python](https://shiny.posit.co/py/) with plain plotly.js, without the shinywidgets layer.

*An independent project, not affiliated with or endorsed by Posit or Plotly.*

[![PyPI](https://img.shields.io/pypi/v/shiny-plotly)](https://pypi.org/project/shiny-plotly/)
[![CI](https://github.com/rvben/shiny-plotly/actions/workflows/ci.yml/badge.svg)](https://github.com/rvben/shiny-plotly/actions/workflows/ci.yml)

**[Try the live demo](https://rvben.github.io/shiny-plotly/)**: a streaming figure and forwarded plotly events, running entirely in your browser through shinylive.

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

Requires Python 3.10+, `shiny>=1.0`, `plotly>=5.5`. Resampling long traces on zoom (`resample=`) also needs numpy: `uv add 'shiny-plotly[resample]'`.

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
    resample=2000,  # draw long line traces as a sample, refreshed on zoom (see below)
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

### Long series: resampling on zoom

A line of a million points costs its full size on the wire and in the browser before anything is drawn, though a chart a few thousand pixels wide can show only a few thousand of them. `resample=` sends a sample instead and keeps the full data on the server:

```python
@render_plotly(resample=2000)
def sensor():
    return go.Figure(go.Scattergl(x=timestamps, y=readings, mode="lines"))
```

Each `scatter` or `scattergl` trace with more points than `resample` is drawn as the minimum and maximum of equal-width buckets, plus both ends and a gap marker wherever the data breaks (a NaN, an infinity, a value at or below zero on a log axis), so spikes survive and a gap is never bridged. The full data stays in the session, on the server. When the user zooms or pans, the browser reports the new range once it has settled for 100 ms, and the server redraws each trace on that axis from the points in view, at the same finite-point budget (gap markers can add points): zoomed in far enough, every point is drawn. Resetting the axes (double click, or the home button) redraws the first sample, which the server keeps rather than recomputes. Shorter traces and other trace types are sent as they are.

- Point events (`click`, `hover`, `selected`) report `pointNumber`, `pointIndex` and `pointNumbers` as positions in the full data, and `customdata`, `text` and every other per-point attribute are sliced along with x and y, so an event means the same as it would on the full trace.
- A box or lasso selection covers the points drawn, not all points in the box; the event's `range` or `lassoPoints` says what was selected, for the server to count against its full data.
- Hover shows the sampled points.
- A re-render with a steady `uirevision` keeps the user's zoom and redraws that zoom from the new data.
- A trace that cannot be sampled faithfully is sent whole, with one warning per render naming it and the reason: x that is not sorted (after converting times to the wall clock plotly.js draws), not numbers or dates, or missing values; y that is not numbers; stacking (`stackgroup`) or a `tonext` fill on its subplot; `xperiod`; per-point error bars; a non-gregorian `xcalendar`; an axis of category type; an x axis with a rangeslider, which would show only the sample. Figures with animation frames or local buttons/sliders using `restyle`, `update`, or `animate` also send their long traces whole, since those controls can replace data without updating the server's sample. Local relayout controls that change a sampled axis's type or rangeslider also fall back to the full trace; local zoom controls remain supported.
- In-place updates that would put the full data and the drawn sample out of step raise `ValueError`: `extend_traces`, `prepend_traces`, `add_traces` and `delete_traces` on the output, a `restyle` of x, y or any per-point attribute of a resampled trace, of `type`, `fill`, `stackgroup` or the axes of any trace, and a `relayout` of a resampled axis's `type` (or an x axis's `rangeslider`). A restyle of a colour, a line width or a name, and any other relayout, work as usual. To change the data, re-render.

A runnable version with a million points is `examples/resample_app.py`.

Repeated broad zooms and pans of very long traces can build a small extrema index on the
third broad view. This adds a one-time build cost and about 0.25–0.375 bytes per source
point, plus full-size temporary buffers during the build; subsequent broad views return
exactly the same points faster. Initial draws and
narrow views use the direct sampler, and densely gapped traces skip indexing. The full
data and any index belong to the session and are replaced when the output re-renders.

### Regular date series

For an unsampled, evenly spaced date series, Plotly's native `x0`/`dx` representation avoids sending and parsing every timestamp. Supply the first date as a string, the interval in milliseconds, and explicitly set a date axis:

```python
fig = go.Figure(go.Scattergl(x0="2026-01-01T00:00:00.123456", dx=60_000, y=values, mode="lines"))
fig.update_xaxes(type="date")
```

The y value at position i represents i minutes after `x0`; missing readings must remain in y as gaps so subsequent dates stay aligned. Date hover formatting, date-string zoom ranges, and point indices retain their normal behavior. The graph data and export describe x with `x0`/`dx` rather than an explicit x array. Use explicit timestamps for irregular intervals and for date-series resampling: `resample=` currently sends date-string `x0` traces whole with a warning.

Numeric epoch coordinates are not an equivalent shortcut: Plotly interprets them in the browser's local timezone and rounds numeric coordinates to 0.1 ms. Their display can differ across browsers, and server-side resampling cannot predict those offsets. Numeric x on a date axis (explicit or inferred from another native-date trace) is therefore sent whole with a warning. Date strings and native datetime arrays retain Plotly's wall-clock semantics.

`make bench-drawing` compares sampling, trace grouping, SVG versus WebGL, and `x0`/`dx` on pinned real datasets. [The benchmark guide](bench/README.md) explains the tradeoffs and captures browser CPU profiles.

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

Plotly alone re-measures a graph only on window resize. `shiny-plotly` ships a small helper script (`shiny-plotly.js`, loaded with every output) that observes each graph's container with a `ResizeObserver`, so a card that changes size without a window resize, for example when a sibling output renders below it, or when a sidebar collapses, re-lays the graph out. When `ResizeObserver` is available, it handles both container and viewport changes and replaces Plotly's per-chart window listener, avoiding duplicate resize requests. Plotly's native listener remains the fallback without an observer. The same helper purges a graph once it leaves the document, releasing its layout state and any fallback listener.

### Many charts on one page

Drawing a plotly figure costs the browser a fixed amount of main-thread work per graph, tens of milliseconds for a small one on a current desktop, and the browser draws them one after another. On a dashboard of a dozen charts that per-chart work, not the bytes on the wire, is what the first second is spent on, and it is plotly's own cost: the same figure drawn from shinywidgets or from a static `to_html` export costs the same.

The lever is drawing fewer charts at once, and Shiny pulls it for you: an output the browser reports as hidden is suspended, so its figure is not rendered at all until it is shown. That covers every container that hides one, whether an inactive panel of `ui.navset_tab`, `ui.navset_card_tab`, `ui.navset_pill` or `ui.navset_hidden`, a closed `ui.accordion` section, or a `ui.panel_conditional` whose condition is false (that last one from Shiny 1.6.1 on; older Shiny drew it at load); each panel then pays only for its own charts, and pays when it is opened. Scrolling is not hiding, though: a chart 3000px down the page is visible as far as the browser is concerned, and is drawn with the rest at load. Charts that must all be visible at once are better served by fewer, denser figures (subplots in one graph div) than by many small ones.

A shared input on a long page can also redraw every chart on every change. Opt in to postpone browser redraws of charts more than 200px outside the viewport:

```python
output_plotly("sales", defer_offscreen=True)
```

An already drawn graph keeps its previous figure while it waits. The latest waiting figure replaces earlier ones, including their queued updates. Updates sent after that figure apply in order after its redraw. Waiting figures draw one at a time during browser idle periods (with a one-second idle timeout, or a timer fallback). Scrolling near an output, pointer entry, and keyboard focus bring its redraw forward. A draw already started completes before another figure for the same output draws.

First draws, empty values, and server errors are never deferred. The server still renders every figure; this option moves off-screen browser drawing out of the immediate response to an input change. Visibility is measured against the viewport, rather than clipping inside a scrolling card. It does not make an individual Plotly redraw interruptible, and background work can still occupy the main thread.

A waiting output carries `shiny-plotly-stale` until its redraw and queued updates finish. It dims after half a second, using a zero-specificity CSS rule. Override it in your app to change the indicator, for example `.shiny-plotly-stale { opacity: 1; }`.

Rapid inputs can also send a new figure before the browser has drawn the previous one.
Opt in to coalesce waiting redraws, including charts in the viewport:

```python
output_plotly("sales", coalesce_renders=True)
# Combine both policies for long dashboards:
output_plotly("history", coalesce_renders=True, defer_offscreen=True)
```

The queue keeps the latest waiting figure and yields between browser draws so incoming
snapshots can replace obsolete work. A draw already started finishes with its trace
updates; replacing a waiting figure discards that figure and its subsequent updates.
Updates sent after the replacement apply to it in order. First draws, empty values and
server errors stay immediate. This reduces browser work during bursts; it still renders
and sends every figure on the server, and cannot interrupt a Plotly draw already running.

Coalescing helps when incoming figures outpace browser drawing. A debounced slider may
already send only the final figure, leaving no draws to skip; queueing can add settling
delay in that case. Leave it disabled when every intermediate figure must be drawn.

Client draw failures are logged and retained for `flush()` to report; a new figure can
recover. Queued outputs use the same stale indicator as offscreen outputs.

Code that reads graph data, such as a button exporting all charts, must first await `window.shinyPlotly.flush()`. For opted-in outputs, it draws waiting figures, waits for redraws already running, and resolves after their queued updates and resizing finish. It rejects if drawing or an update fails; a new figure clears that failure. Updates arriving after a failure are dropped with a warning until a new figure arrives, so a stream cannot build an unusable queue. The promise covers browser draws; it does not wait for server resampling answers requested by a preserved zoom. A resampled figure may still show its overview while that answer is in flight, so `flush()` alone cannot guarantee zoom detail in an export. For a snapshot of the figures currently available in the browser before programmatic printing:

```js
await window.shinyPlotly.flush();
window.print();
```

Native print requests also start a refresh, but browsers do not wait for promises returned by `beforeprint` listeners, so asynchronous drawing may finish after the printed snapshot is taken.

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

How it works: both templates get their `paper_bgcolor` and `plot_bgcolor` made transparent, so the card's own background shows through in both modes (backgrounds set on the figure's layout still win). The browser applies the mode's template before the first draw and switches it with `Plotly.relayout` when the mode flips, so the switch is instant and works even while the server is busy. A template the figure baked in through `layout.template` is dropped for themed outputs; use `theme=None` (the default) where the figure's own template should stand.

A template is about 6.5 kB of JSON, so it is sent **once per session** rather than once per render: the first themed value of a session brings the templates over their own message, and every value after it names them by content hash. A dashboard of twelve charts on one theme therefore pays for the theme once, and a re-render of a small figure is not dominated by a template that has not changed. Two outputs whose templates are equal share the copy automatically; nothing has to be declared.

The mode comes from `data-bs-theme` on the **nearest ancestor of the output that sets one**. Usually that is `<html>`, which is what `ui.input_dark_mode()` maintains, and a page without the attribute anywhere follows the OS `prefers-color-scheme`. Because the lookup is per output, a container can theme the charts inside it against the rest of the page:

```python
ui.div(
    ui.card(output_plotly("preview")),  # always dark, whatever the page does
    data_bs_theme="dark",
)
```

**Driving the mode yourself.** The attribute is the whole contract, so anything that sets it switches the charts: no shiny-plotly API is involved. To drive it from your own control, write it and the graphs follow on the next frame:

```python
app_ui = ui.page_fluid(
    ui.input_switch("night", "Night mode"),
    ui.tags.script("""
        Shiny.addCustomMessageHandler("color-mode", function (message) {
          document.documentElement.setAttribute("data-bs-theme", message.mode);
        });
    """),
    ui.card(output_plotly("sales")),
)


@reactive.effect
async def _apply_mode():
    mode = "dark" if input.night() else "light"
    await session.send_custom_message("color-mode", {"mode": mode})
```

The same holds in reverse: a page that already themes itself (a CSS framework, a cookie read at startup, `ui.input_dark_mode()` in a nav bar) needs nothing added, and a chart rendered while the attribute is already `dark` comes up dark on its first draw rather than flashing light first.

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

Each point carries plotly's scalar fields for that trace type (`curveNumber`, `pointNumber`, `pointIndex`, `x`, `y`, `z`, `text`, `label`, `value`, `lat`, `lon`, ...) plus `customdata` (as a plain list, also when it was a numpy array), `bbox` and `pointNumbers` when present. On a 2-D trace such as a heatmap `pointNumber` is the cell's `[row, column]`. `input.<id>_<event>()` raises a silent exception until the event has fired once, so check `is_set()` when the output should show something before that.

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

For anything else, `post_script` runs once, after the first figure is drawn, with `{plot_id}` replaced by the graph div's id. A script that throws is logged to the browser console and the chart carries on: events, theming and held updates do not depend on it. Re-renders go through `Plotly.react` into the same graph div, so handlers attached either way stay attached and are never stacked.

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

For figures with many traces, use an explicit update when only some series change. [The many-trace example](examples/many_traces_app.py) draws once, then calls `update()` with one trace’s y values and a new title in one redraw. Unchanged traces stay in the browser, and a user’s zoom is preserved. `make bench-updates` compares this path with a full re-render on a pinned 505-trace stock dataset; [benchmark instructions](bench/README.md) describe setup and measurement boundaries. Resampled traces still require a re-render when their data changes.

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

The values go through plotly's encoder, so numpy arrays, pandas columns and datetimes work. The id is namespaced inside a module, like the output. An update reaches the figure that is drawn at that moment; one sent while the output has no figure (its first render is still running, it sits in a hidden tab, it shows an error or was emptied by `None`) is held and applied, in order, right after the output's next draw. Consecutive held `extend_traces` calls with the same traces, attributes and `max_points` are merged into one as they arrive, and so are consecutive `prepend_traces` calls, cut to `max_points` as they go: a stream into a chart in a closed tab costs one redraw when the tab opens and, with a cap, holds no more points than the chart will keep. A re-render replaces the figure, updates included, with what the render function returns: the server stays the source of truth, and a figure that should keep its streamed points across a re-render builds them in from server-side state.

A runnable version of the streaming pattern, with a pause switch and a window slider, is `examples/streaming_app.py`.

### Lower level

- `fig_to_ui(fig, div_id=None, *, height, width, figurewidget_margins, config, post_script)` returns a `TagList` holding the plotly.js dependency, the helper dependency and a `<div class="shiny-plotly">` that draws the figure with `Plotly.newPlot` (plotly's own `to_html` fragment). Use it from a plain `@render.ui` that composes a figure with other UI, or from any htmltools context. Each render draws a fresh graph; an output that is only a figure is better served by `render_plotly`.
- `plotly_js()` is the `HTMLDependency` for plotly.js, served from the installed `plotly` wheel at `/lib/plotly-<version>/plotly.min.js`. Every `output_plotly` and every `fig_to_ui` fragment carries it, so it is optional; add it to the page UI when the first figure is inserted later (`ui.insert_ui`, a `@render.ui` that starts empty) and the bundle should load with the page.
- `shiny_plotly_js()` is the helper's dependency. Every output and fragment carries it too.
- `FIGUREWIDGET_MARGINS` is the `{"l": 16, "t": 32, "r": 16, "b": 16}` mapping.
- `enable_compressed_plotly_js(app)` adds compressed, immutable serving of plotly.js to a `shiny.App` explicitly. Every app built after `shiny_plotly` is imported already has it; this is the way in for one that was constructed before the import (see below).
- `extend_traces`, `restyle` and `relayout` take an optional `session=` when called outside the current session's context.

`render_plotly` needs `output_plotly`; it is an output binding, not a `render.ui`, so `ui.output_ui(id)` does not draw it.

### plotly.js on the wire

Shiny serves HTML dependencies from a plain static mount: no compression, no `Cache-Control`. `plotly.min.js` is 4.9 MB, so `shiny-plotly` adds a route in front of that mount for the bundle's exact path (`/lib/plotly-<version>/plotly.min.js`) that serves it pre-compressed (brotli, 1.2 MB on the wire, or gzip at 1.5 MB where brotli is not installed) with `Cache-Control: public, max-age=31536000, immutable`, `Vary: Accept-Encoding` and an `ETag` per encoding. The URL is keyed by the plotly version, so a browser fetches each version once. Compression runs in a background thread when no shared cached encoding is available; until it has finished the route serves the raw file, with `Cache-Control: no-cache` to a browser that asked for an encoding still being compressed, so its next visit revalidates and swaps in the compressed body rather than keeping the raw one for a year.

Compressed encodings are also cached on disk and shared across workers and restarts.
A warm worker loads them before its first response, avoiding another compression pass.
The cache lives in the user's OS cache directory (`~/Library/Caches/shiny-plotly` on macOS,
`$XDG_CACHE_HOME/shiny-plotly` or `~/.cache/shiny-plotly` on Linux, and
`%LOCALAPPDATA%/shiny-plotly` on Windows). Set `SHINY_PLOTLY_CACHE_DIR=/path/to/cache`
to use another directory, such as a local persistent volume shared by app workers.
Use a local filesystem with reliable SQLite locking; separate hosts need separate caches.

Entries are keyed by the bundle's SHA-256, encoding, compression settings and codec version.
SQLite transactions coordinate simultaneous producers and prevent partial writes; cached
payload checksums detect corruption. The database is capped at 64 MiB and 32 encodings,
evicting the oldest entries as needed; its transaction journal uses temporary additional
space. An encoding larger than 8 MiB is served without being stored. Cache misses compress
in the background as before. If the cache is busy, unavailable, full or corrupt, the app
falls back to local compression. Set `SHINY_PLOTLY_NO_CACHE=1` to disable disk caching
while retaining compression; `SHINY_PLOTLY_NO_COMPRESS=1` disables the route entirely.
`make bench-compression` measures cold and warm workers over local HTTP, including the
first asset response and bytes transferred; `COMPRESSION_ARGS="--bundle plotly-basic.min.js"`
measures a partial bundle. These timings exclude Python imports and browser evaluation.

`brotli` is a dependency, so a plain `uv add shiny-plotly` serves the smaller encoding. It is skipped under pyodide, where there is nothing to compress: a shinylive export carries its own assets and the route is not installed at all. An install that ends up without it (a lock file that predates the dependency, a platform with no wheel) falls back to gzip and logs one warning saying which encoding it is serving and what brotli would save, so a deployment can see it is shipping the larger bundle; `logging.getLogger("shiny_plotly").setLevel(logging.ERROR)` silences it.

The route asks nothing of the app. Importing `shiny_plotly` wraps `shiny.App.__init__`, so every app built afterwards has it, Core and Express alike, and the compression starts while the app is still being built rather than when someone first visits it. The timing is the whole point: the browser asks for plotly.js while the page is loading, well before the session that page opens exists, so a route that waited for a session would arrive one visitor too late, and that visitor would take 4.9 MB with no `Cache-Control` at all.

An app constructed before `shiny_plotly` is imported is the one case the constructor cannot reach; `enable_compressed_plotly_js(app)` adds the route to it, and returns `False` if it is already there.

If a reverse proxy in front of the app does its own compression and caching, or you want Shiny's static serving untouched for any reason, set `SHINY_PLOTLY_NO_COMPRESS=1` in the app's environment: no route is added, and `enable_compressed_plotly_js` returns `False`.

### A partial plotly.js bundle

For dashboards that use only a few trace types, a partial plotly.js bundle reduces the
JavaScript the browser downloads and evaluates. The `basic` bundle includes scatter,
bar and pie. Select it at the top of the app file, before building any plotly output,
`fig_to_ui` fragment or explicit `plotly_js()` dependency:

```python
from shiny_plotly import use_plotly_bundle

use_plotly_bundle("plotly-basic.min.js")
```

Alternatively, set `SHINY_PLOTLY_BUNDLE=/path/to/plotly-basic.min.js` in the app's
environment. An explicit call takes precedence. The choice applies to the whole process
and is fixed by its first plotly dependency; choosing the same content again is harmless.
Constructing an unrelated Shiny app does not fix the choice.

The bundle must match the plotly.js version the installed plotly targets. Download that
version, rather than the latest release:

```sh
v=$(python -c "from plotly.offline import get_plotlyjs_version; print(get_plotlyjs_version())")
curl -fsSLo plotly-basic.min.js "https://cdn.jsdelivr.net/npm/plotly.js-basic-dist-min@$v/plotly-basic.min.js"
```

A mismatched version raises `ValueError` with the expected version and download URL.
Other partial bundles, such as `cartesian`, `finance` and `geo`, and custom dist builds
work the same way. Custom builds must be rebuilt when plotly's target version changes;
keep their plotly.js version banner and trace-module metadata intact.

The file is read and validated once when chosen. The exact bytes are copied into a private
temporary directory, so Shiny never exposes neighboring app files, and later changes or
removal of the original file cannot change what is served. The dependency URL includes the
variant and a SHA-256 digest, `/lib/plotly-<version>+basic.<digest>/plotly.min.js`. It uses
the same compressed, immutable route as the full bundle, including for callable UI.
With `SHINY_PLOTLY_NO_COMPRESS=1`, Shiny serves the same private copy without that route.

Every rendered figure and `fig_to_ui` fragment, including animation frames, is checked
against the trace types found in the bundle. The same check covers `add_traces` and type
changes sent through `restyle` or `update`, including NumPy arrays. A missing type raises
`ValueError`, naming the missing types and listing those available; render errors appear
in the output. An untyped trace defaults to scatter. An omitted frame type inherits the
trace it animates; an explicit `None` type resets to scatter, as does a type reset in
`restyle` or `update`.

### Shinylive

Apps using `shiny-plotly` run under [Shinylive](https://shiny.posit.co/py/get-started/shinylive.html) (pyodide in the browser) as well; list `shiny-plotly` in the app's `requirements.txt` next to `plotly`. There is no HTTP server in the browser and pyodide cannot start threads, so the compression route above stands down under pyodide (`enable_compressed_plotly_js` returns `False`); everything else, rendering, events, in-place updates and themes, is browser-side already. Verified against a real `shinylive export`; `examples/shinylive/` is a ready-to-export app, and its deployed copy is the [live demo](https://rvben.github.io/shiny-plotly/). The Pages workflow builds that demo from the current checkout's wheel (`make site`) and deploys only after a headless Chromium has watched both tabs render (`make site-check`).

## Examples

```sh
uv run --with shiny-plotly shiny run examples/core_app.py     # fill, margins, a click input, a live stream
uv run --with shiny-plotly shiny run examples/express_app.py  # the Express flavor
uv run --with shiny-plotly shiny run examples/dark_app.py     # theme="auto" and a custom (light, dark) pair
uv run --with shiny-plotly shiny run examples/events_app.py   # box selections over a 50k-point trace
uv run --with shiny-plotly shiny run examples/streaming_app.py # a rolling window fed by extend_traces
uv run --with 'shiny-plotly[resample]' shiny run examples/resample_app.py # a million points, resampled on zoom
uvx shinylive export examples/shinylive site                  # the same package, running in the browser
```

## Development

```sh
make sync        # uv sync --all-groups
make browsers    # playwright install chromium, once
make check       # lint, typecheck, unit + e2e tests, browser tests, wheel check, floor check
make bench       # the shinywidgets comparison above, on this machine
make bench-events  # what a selection over a dense trace costs, capped and uncapped
make bench-resample  # server rendering, sampling, and repeated broad views
```

`make bench-resample` measures finite, sparsely gapped, densely gapped, and fully missing
traces at one and five million points. It reports initial rendering, direct sampling,
the first three view updates (including any index build), steady view updates, and
retained index size, and whether indexing was enabled. At the default budget, the
one-million-point views exercise the direct sampler; the five-million-point finite and
sparse-gap views build an index and check that indexed and direct payloads match exactly.
These are server timings; browser drawing and network latency are excluded. Compare
commits on an idle machine with the same Python and dependency versions, and compare
the payload hashes alongside the timings. Save results outside the checkout with, for
example, `make bench-resample BENCH_ARGS="--repeats 35 --output /tmp/resample.json"`.

`make test` runs the unit tests and the in-process Shiny end-to-end tests over a real websocket, including the compressed bundle route. It fails below 100% line and branch coverage of the package: every line is reachable without a browser, and the gate is what keeps behavior that only the Chromium suite can reach from growing. `make test-browser` drives the package in headless Chromium: fill sizing, resize without a window event, the graph div surviving a re-render, `uirevision` keeping a dragged zoom, purge once an output leaves the page, full screen, `events=` click, hover, selection and relayout inputs (attached once, also inside a module, a selection above `max_event_points` arriving as count and range), `extend_traces`, `restyle` and `relayout` applied in place (rolling window, one trace or all, held until the first draw, reset by a re-render, inside a module, dropped with a warning for an unknown output), `post_script` click wiring (once, not stacked), the dark mode recipe, error and `None` rendering, on-demand loading of plotly.js and the compressed, cached bundle as a fresh visitor sees it. `make check-wheel` installs the built wheel into a throwaway venv and runs the suite against it, so the published artifact is what was tested. `make check-floor` installs the package with plotly, shiny and htmltools at the oldest versions `pyproject.toml` allows and runs the whole suite again, browser tests included, so the declared lower bounds are tested on every push rather than assumed.

## License

MIT. See [LICENSE](LICENSE).
