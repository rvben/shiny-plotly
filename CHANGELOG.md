# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/rvben/shiny-plotly/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rvben/shiny-plotly/releases/tag/v0.1.0
