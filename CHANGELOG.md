# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- README: the note on hidden outputs now names every container Shiny defers a chart in (an inactive `ui.navset_tab`, `ui.navset_card_tab`, `ui.navset_pill` or `ui.navset_hidden` panel, a closed `ui.accordion` section, and a false `ui.panel_conditional` from Shiny 1.6.1 on, which is where that last one changed), and says what it does not cover: a chart scrolled below the fold is drawn with the rest at load. A browser test holds every case, so a Shiny release that changes the behavior breaks the suite rather than the advice.

## [0.3.3](https://github.com/rvben/shiny-plotly/compare/v0.3.2...v0.3.3) - 2026-08-21

### Added

- A themed chart takes its color mode from the nearest ancestor that carries `data-bs-theme`, itself included, instead of from `<html>` alone. Bootstrap honors the attribute on any element, so `ui.div(..., data_bs_theme="dark")` around part of a page now themes the charts inside it while the rest of the page follows the page mode. The observer watches the whole subtree, so the attribute can be set on a container after the charts are drawn; that is all a control of your own needs to drive the mode, and the README carries the recipe.
- A warning, said once per process, when `plotly.min.js` is served gzipped because `brotli` is not installed: it names the size being served and the roughly 17% brotli would save. Without it a deployment has no way to notice, since the bundle is compressed, cached and immutable either way, just bigger. Silence it with `logging.getLogger("shiny_plotly").setLevel(logging.ERROR)`.
- README: what each chart on a page costs to draw, and that Shiny suspends outputs hidden in an inactive `ui.nav_panel`, so charts in tabs are drawn when their tab is first opened rather than on load.

### Changed

- Every app serves plotly.min.js compressed and immutable from its first request, with no call of its own: importing `shiny_plotly` wraps `shiny.App.__init__`, so the route is added as each app is built, Core and Express alike. Until now it arrived with the first session, which is after the page load that opened that session had already pulled 4.9 MB from Shiny's static mount with no `Cache-Control` at all; the background compression now starts while the app is being built instead of when the first visitor is already waiting. `enable_compressed_plotly_js(app)` stays for an app constructed before the import, `SHINY_PLOTLY_NO_COMPRESS=1` still leaves Shiny's static serving untouched, and a failure to install the route is logged rather than raised: it can never keep an app from being built.
- The templates behind `@render_plotly(theme=...)` travel once per session instead of once per figure. A template is about 6.5 kB, and every chart on a page usually shares one pair, so the first themed value carries the pair and every value afterwards names it by content hash: a page of ten charts sends about 13 kB of templates instead of 135 kB. Two outputs on different themes each send their own pair, a module shares the cache with the page around it, and a reconnecting browser gets a new session and so a fresh copy. A render with no session to cache against (a value produced outside a session, or Express's stub session before it connects) still carries its templates inline.

## [0.3.2](https://github.com/rvben/shiny-plotly/compare/v0.3.1...v0.3.2) - 2026-08-20

### Added

- Three more forwardable events: `doubleclick` arrives as a running count of plot-area double-clicks (plotly hands the event no data; the count's change invalidates the input); `legendclick` and `legenddoubleclick` arrive as the clicked trace's `curve_number`, `expanded_index`, `name` and pre-toggle `visible`, plus `label` for trace types whose legend items are labels. The default toggle and isolate behaviors still happen.
- `examples/streaming_app.py`: a live rolling window fed by `extend_traces`, with a pause switch and a window slider; a browser test drives the example file itself and proves the stream flows and pauses.
- A live demo at https://rvben.github.io/shiny-plotly/: `examples/shinylive/app.py` grew into a two-tab showcase (a streaming figure, click and box-select events, a dark mode toggle), deployed to GitHub Pages by a workflow that builds the site from the checkout's own wheel (`make site`) and gates the deploy on a headless Chromium watching both tabs render (`make site-check`). The committed example still installs from PyPI, so `uvx shinylive export examples/shinylive site` keeps working as documented.

## [0.3.1](https://github.com/rvben/shiny-plotly/compare/v0.3.0...v0.3.1) - 2026-08-20

### Added

- `@render_plotly(theme="auto")`: the figure follows the page's color mode in the browser, with no server round-trip. `"auto"` pairs plotly's `"plotly"` and `"plotly_dark"` templates; a `(light, dark)` tuple picks others, each a registered name, a `Template` object or a template dict. Both templates travel with the figure with transparent backgrounds so the page shows through; the browser applies the mode's template before the first draw and switches it with `Plotly.relayout` when `data-bs-theme` (what `ui.input_dark_mode()` sets) or the OS `prefers-color-scheme` flips. Unknown template names raise at decoration time.
- The in-place update family is complete: `add_traces`, `delete_traces`, `prepend_traces` and `update` (a restyle and a relayout in one redraw) join `extend_traces`, `restyle` and `relayout`. Same contract as the first three: values through plotly's encoder, ids namespaced inside a module, updates held and applied in order while the output has no figure drawn.
- Examples for dark mode (`examples/dark_app.py`), dense-trace selections (`examples/events_app.py`) and a ready-to-export shinylive app (`examples/shinylive/`); a test keeps every example building.

### Fixed

- Shinylive: under pyodide the compressed plotly.js route is skipped instead of crashing the session. Pyodide cannot start threads, so the compression thread raised `RuntimeError: can't start new thread` from the first `@render_plotly` of every session and no figure ever rendered; there is also nothing to serve there, as the browser loads assets from the shinylive bundle. Verified against a real `shinylive export`: 0.3.0 crashes, this version renders.

## [0.3.0](https://github.com/rvben/shiny-plotly/compare/v0.2.0...v0.3.0) - 2026-08-20

### Added

- `@render_plotly(events=...)` forwards plotly events to Shiny inputs: any of `click`, `hover`, `selected` and `relayout` arrive as `input.<id>_<event>` (namespaced inside a module), carrying plotly's event data cut to what serializes (`points` with each point's scalar fields plus `customdata`, `bbox`, `pointNumbers`; `range` or `lassoPoints` for selections; relayout data as is). A click fires on every click; hover is debounced and becomes `None` when the pointer leaves; a deselect sets `selected` to `None`. Handlers attach once per graph div and survive re-renders. `max_event_points` (default 10 000) caps the points one event carries: above it the value arrives with `"points": None` and `point_count` set, its `range` or `lassoPoints` intact, because a point is about 100 bytes of JSON and a box over a dense trace would otherwise build a message of many megabytes, or one above uvicorn's default 16 MB websocket limit, which closes the session (`make bench-events` measures it; 200 000 selected points are 20.9 MB).
- `extend_traces(id, data, indices=None, *, max_points=None)`, `restyle(id, update, indices=None)` and `relayout(id, update)`: in-place updates to the figure an output holds, sent as Shiny custom messages and applied in the browser with `Plotly.extendTraces`, `Plotly.restyle` and `Plotly.relayout`. Values go through plotly's encoder; the id is namespaced inside a module; an update sent while the output has no figure drawn is held and applied after its next draw; a re-render replaces the figure, updates included.
- `enable_compressed_plotly_js(app)` is public: a Core app can serve plotly.js compressed and immutable from its very first request instead of from its first session on.

### Fixed

- `output_plotly()` now carries the htmltools fill CSS itself. On a page that did not load it otherwise (`ui.page_fluid` without a card), a fixed-height output (`output_plotly(id, height="200px")`) with a bare `@render_plotly` drew a 400px graph that overflowed the output and whatever sat below it.
- `output_plotly(id)` (and the Express auto output) did not namespace its id inside a Shiny module, so a `@render_plotly` in a module never found its output.
- The declared dependency floor was wrong: `plotly>=5.0` and `htmltools>=0.5` could not work (`fig_to_ui` needs `to_html(div_id=...)`, which plotly 5.5 introduced; shiny 1.0 itself needs htmltools 0.5.2). The bounds are now `plotly>=5.5`, `htmltools>=0.5.2`, and CI installs exactly that floor (with shiny 1.0) and runs the whole suite against it, browser tests included (`make check-floor`).

## [0.2.0](https://github.com/rvben/shiny-plotly/compare/v0.1.0...v0.2.0) - 2026-08-19

### Changed

- `@render_plotly` is now a Shiny output binding instead of a `render.ui`: the figure travels as plotly JSON and `shiny-plotly.js` draws it with `Plotly.newPlot` the first time and `Plotly.react` on every re-render, into one graph div it keeps. Handlers attached by `post_script` stay attached (it runs once, not on every render), a figure with `layout.uirevision` keeps the user's zoom and pan across re-renders, a render error shows in place and `None` empties the output. Trace data is serialized by plotly's encoder, so numpy arrays, pandas columns and datetimes work as they do in `fig.to_json()`.
- `output_plotly()` carries the plotly.js and helper dependencies itself, so a page-level `plotly_js()` call is no longer needed for it.
- **Breaking:** `@render_plotly` requires `output_plotly(id)`; `ui.output_ui(id)` no longer draws it. `fig_to_ui` is unchanged and remains the path for `@render.ui`.

### Added

- `plotly.min.js` is served pre-compressed (brotli with the optional `brotli` extra, gzip otherwise) with `Cache-Control: public, max-age=31536000, immutable`, `Vary: Accept-Encoding` and an `ETag` per encoding, through a route added in front of Shiny's static mount when the first session renders a figure. Compression runs once per process in a background thread. `SHINY_PLOTLY_NO_COMPRESS=1` turns the route off.
- `shiny-plotly[brotli]` extra.
- Bench: one warm-up visit per server before measuring, HTTP accounting from the browser's Resource Timing (cache hits count as zero bytes), and rows for plotly.js on the wire and requests that left the cache on a repeat visit.

## [0.1.0] - 2026-08-19

### Added

- `@render_plotly`: a `shiny.render.ui` subclass that renders a `plotly.graph_objects.Figure` (or its dict) as HTML drawn with `Plotly.newPlot`. Works in Core and Express, sync or async, bare or with options; `None` renders nothing.
- `output_plotly(id, width=, height=)`: fill-aware placeholder, a drop-in for `output_widget`. No height fills the container from a 400px basis; an explicit height is fixed and opts out of filling.
- `fig_to_ui(...)`: the lower-level fragment builder for use from `@render.ui` or any htmltools context.
- `plotly_js()`: `HTMLDependency` serving plotly.js straight from the installed `plotly` wheel at `/lib/plotly-<version>/plotly.min.js`; every figure carries it, so a page-level call is optional.
- `shiny_plotly_js()` and the `shiny-plotly.js` helper: a `ResizeObserver` keeps each graph sized to its container when the container changes without a window resize, and purges a graph once Shiny replaces the output holding it.
- `figurewidget_margins=True` fills in unset margin sides with the values shinywidgets applies to a `FigureWidget` (l16/t32/r16/b16), for pixel-identical migrations.
- `config` (merged over `{"responsive": True}`) and `post_script` (with `{plot_id}` substitution) options for forwarding plotly events to Shiny inputs.
- Test suite: unit tests, in-process Shiny end-to-end tests over a real websocket, and a headless Chromium suite covering sizing, resize tracking, purge on re-render, full screen and `post_script` wiring.
