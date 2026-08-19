# shiny-plotly

Render [plotly](https://plotly.com/python/) figures in [Shiny for Python](https://shiny.posit.co/py/) as plain HTML, without the shinywidgets layer.

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

That is the whole API surface for the common case. The figure is serialized with plotly's own `to_html`, sent to the browser as a UI fragment, and drawn with `Plotly.newPlot`. No ipywidgets, no kernel comm, no anywidget. Every render replaces the figure, which is how most dashboards already use `@render_widget`.

## Why

`shinywidgets` renders a plotly figure by wrapping it in a `FigureWidget` and shipping it through the ipywidgets comm protocol. That machinery earns its keep when the app mutates a figure in place (`fig.data[0].y = ...`) and wants the browser to patch it. Most Shiny apps do not do that; they rebuild the figure inside a reactive function and let Shiny re-render the output. For those apps the widget layer is overhead:

- extra dependencies (`ipywidgets`, `anywidget`, `shinywidgets`) and their JavaScript bundles on every page;
- a second rendering path next to Shiny's own, with its own quirks around sizing and full screen;
- figures held as widget state on the server for the life of the session.

`shiny-plotly` renders the figure the way plotly itself does, as HTML plus `Plotly.newPlot`, and uses Shiny's `render.ui` for delivery. The plotly.js bundle is served straight from the installed `plotly` wheel, keyed by its version, so nothing is copied or vendored.

## Install

```sh
uv add shiny-plotly
# or
pip install shiny-plotly
```

Requires Python 3.10+, `shiny>=1.0`, `plotly>=5.0`.

## Use

### Core

```python
import random
from itertools import accumulate

import plotly.graph_objects as go
from shiny import App, ui

from shiny_plotly import output_plotly, plotly_js, render_plotly

app_ui = ui.page_fillable(
    ui.input_slider("n", "Points", 10, 500, 100),
    ui.card(
        ui.card_header("Fills the card; try full screen"),
        output_plotly("walk"),
        full_screen=True,
    ),
    plotly_js(),  # optional: load plotly.js with the page instead of with the first figure
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
    post_script=CLICK_TO_INPUT,  # JavaScript run after the plot is drawn
)
def sales(): ...
```

`None` from the render function renders nothing. The function may be sync or async. It may also return `fig.to_dict()` instead of a `Figure`.

### Migrating from shinywidgets

| shinywidgets | shiny-plotly |
| --- | --- |
| `from shinywidgets import output_widget, render_widget` | `from shiny_plotly import output_plotly, render_plotly` |
| `output_widget("id")` | `output_plotly("id")` |
| `output_widget("id", height="300px")` | `output_plotly("id", height="300px")` |
| `@render_widget` | `@render_plotly` |
| (FigureWidget margins, applied implicitly) | `@render_plotly(figurewidget_margins=True)` |

Two things change on purpose:

- **Margins.** shinywidgets sets tight margins (`l=16, t=32, r=16, b=16`) on every FigureWidget; plotly's own defaults are `80/100/80/80`. `shiny-plotly` uses plotly's defaults unless you pass `figurewidget_margins=True`, which fills in only the sides your figure leaves unset. Set margins explicitly on the figure if you want something else.
- **In-place mutation.** A `FigureWidget` you keep on the server and mutate (`fig.data[0].y = ...`, `fig.add_trace(...)` after render) is exactly what shinywidgets is for. `shiny-plotly` has no channel for that; return a new figure from the render function and let Shiny re-render. If your app depends on in-place widget updates, stay on shinywidgets for those outputs. Both packages can coexist in one app.

### Sizing

The rules mirror `output_widget`:

- `height=None` (default): the plot fills its container. Inside `ui.card(full_screen=True)`, a fillable page or a sidebar layout it grows and shrinks with the card, from a 400px basis. Outside a fill layout it is 400px tall.
- `height="300px"` (on the decorator or on `output_plotly`): the plot is exactly that tall and opts out of filling.

Plotly alone re-measures a graph only on window resize. `shiny-plotly` ships a small helper script (`shiny-plotly.js`, loaded with every figure) that observes each graph's container with a `ResizeObserver`, so a card that changes size without a window resize, for example when a sibling output renders below it, or when a sidebar collapses, re-lays the graph out. The same helper purges a graph once Shiny has replaced the output holding it, which releases the window listener and layout state plotly would otherwise keep for every render.

### Events back to Shiny

`post_script` runs after `Plotly.newPlot` resolves; `{plot_id}` is replaced with the graph div's id.

```python
CLICK_TO_INPUT = """
document.getElementById('{plot_id}').on('plotly_click', function (ev) {
    var p = ev.points[0];
    Shiny.setInputValue('clicked', {x: p.x, y: p.y}, {priority: 'event'});
});
"""


@render_plotly(post_script=CLICK_TO_INPUT)
def scatter(): ...


@render.text
def click_info():
    if not input.clicked.is_set():
        return "Click a point."
    pt = input.clicked()
    return f"x={pt['x']}, y={pt['y']}"
```

`input.clicked()` raises a silent exception while the input has never been set, so check `is_set()` first when the output should show something before the first click.

### Lower level

- `fig_to_ui(fig, div_id=None, *, height, width, figurewidget_margins, config, post_script)` returns the `TagList` a render produces: the plotly.js dependency, the helper dependency and the `<div class="shiny-plotly">` holding the figure. Use it from a plain `@render.ui` that composes several things, or from any htmltools context.
- `plotly_js()` is the `HTMLDependency` for plotly.js, served from the installed `plotly` wheel at `/lib/plotly-<version>/plotly.min.js`. Every figure carries it, so it is optional; add it to the page UI to load the bundle up front instead of with the first figure.
- `shiny_plotly_js()` is the helper's dependency. Every figure carries it too.
- `FIGUREWIDGET_MARGINS` is the `{"l": 16, "t": 32, "r": 16, "b": 16}` mapping.

## Examples

```sh
uv run --with shiny-plotly shiny run examples/core_app.py
uv run --with shiny-plotly shiny run examples/express_app.py
```

## Development

```sh
make sync        # uv sync --all-groups
make browsers    # playwright install chromium, once
make check       # lint, typecheck, unit + e2e tests, browser tests, wheel check
```

`make test` runs the unit tests and the in-process Shiny end-to-end tests over a real websocket. `make test-browser` drives the package in headless Chromium: fill sizing, resize without a window event, purge on re-render, full screen, `post_script` click wiring and on-demand loading of plotly.js. `make check-wheel` installs the built wheel into a throwaway venv and runs the suite against it, so the published artifact is what was tested.

## License

MIT. See [LICENSE](LICENSE).
