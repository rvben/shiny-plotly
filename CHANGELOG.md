# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
