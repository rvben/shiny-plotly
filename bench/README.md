# Performance workloads

Use an idle machine, the same Python/dependency versions and warmed operations for
comparisons. Server transform time and browser interaction latency are different
measurements. Results go to stdout; --output writes a local JSON artifact. Keep
downloaded fixtures and generated results outside Git.

The synthetic sampler benchmark can use native microsecond or nanosecond dates:

    make bench-resample BENCH_ARGS="--points 5000000 --date-unit us --repeats 15"

`--date-unit ns` checks the unit-conversion path with the same one-second readings
starting at 2026-01-01; omitting the option keeps numeric x.
View timings use the resulting date-axis coordinates and verify that lazy indexing
preserves the direct sampler's complete payload. These are server measurements,
not browser interaction timings.

## Real recordings

The sources in realworld.py are pinned to GitHub commits and SHA256 checksums:

- [Plotly Resampler's file-selector example](https://github.com/predict-idlab/plotly-resampler/blob/989043e53b905a472fc36db31d3f715c323a6585/examples/dash_apps/12_file_selector.py)
  provides a power-meter recording (3 × 405,537 points) and a building recording
  (3 × 194,046 points, missing readings, Europe/Brussels timezone and DST).
  Their reversed-column Scattergl construction is adapted to ordinary Plotly
  figures rendered by shiny-plotly. This does not benchmark the upstream engine.
- [Plotly's five-year stock dataset](https://github.com/plotly/datasets/blob/0c447c47b757ad74edecab31f0d72f849d2e67c2/all_stocks_5yr.csv)
  supplies 505 traces and 619,040 points. One close-price trace per symbol makes
  a many-trace control; it is a new stress figure built from real data.

Prepare the cache once with pyarrow (which is not a package dependency):

    uv run --with pyarrow python -m bench.realworld --fetch --prepare

Then measure the renderer's complete transform after warmup:

    make bench-realworld BENCH_ARGS="--repeats 7"
    make bench-realworld BENCH_ARGS="--repeats 7 --native-dates"

Construction, loading, garbage collection and payload hashing are outside the
timed transform. The sensor recordings use resample=2000 per trace; stocks are
unsampled. Native dates are an input-representation control: they preserve the
tested wall-clock coordinates but change serialized date spelling and offsets.

The prepared cache uses numeric arrays with allow_pickle=False, epoch
microseconds and a timezone identifier. It preserves the source numeric dtypes
and reconstructs the original timestamps, including DST folds. Prepare on a
Python version with a pyarrow wheel, then use the same cache with Python 3.15:

    PYTHONPATH=src /path/to/python3.15 -m bench.realworld --repeats 7

For Tachyon, use Python 3.15's profiling.sampling run with --native --binary and
replay the capture as a flamegraph. Check the child result, source path and
sampling error rate before interpreting profiles. The complete script includes
startup and construction; unprofiled repeated transforms establish speedups.

## Many-trace updates

Compare a full re-render against update() of one trace and its title:

    make bench-updates BENCH_ARGS="--repeats 5"

Run make browsers first. The local Shiny app uses the cached stock fixture.
The paths alternate order after warmup; both change the same series and title.
Each operation gets a new change number, and the benchmark checks the drawn
value and title after promise completion. It reports input-to-draw and Plotly
promise times, received WebSocket application bytes, and uncaught page errors.
The bytes are before transport compression, not actual network bandwidth.

See examples/many_traces_app.py for a smaller runnable version. Explicit updates
leave unchanged traces on the client and avoid a full-figure message. They do
not promise a redraw proportional to the changed trace: Plotly may recalculate
other traces. Resampled outputs still reject data updates that would invalidate
their retained full data; re-render those outputs instead.


## Browser drawing and profiles

    make bench-drawing BENCH_ARGS="--cases stocks_gl stocks_sampled power_regular power_regular_steps --repeats 7 --output /tmp/drawing.json --profiles /tmp/plotly-profiles"
    make bench-updates BENCH_ARGS="--case power_regular_steps --repeats 7 --profiles /tmp/shiny-profiles"

Both browser benchmarks default to Playwright's `channel="chromium"` (new headless
mode), using its downloaded Chrome for Testing rather than system Chrome. Run
`make browsers` to install it. Results record the Chromium version, Plotly.js
version, pixel ratio and WebGL renderer. `--headless-shell` reproduces Playwright's
older shell; it may use SwiftShader software rendering. Compare results only on
the same renderer and browser mode. Software-rendered WebGL can be dramatically
slower than a hardware-backed browser.

`bench.drawing` prepares payloads with the real renderer, then calls Plotly
in a standalone page. Its timers exclude Shiny, transport, JSON parsing and
construction of changed arrays. It measures promise completion, not GPU completion
or time to a physically presented frame. A separate two-animation-frame duration
is reported; it is a browser rendering checkpoint, not a GPU completion fence.
React timings include Plotly’s binary-array decoding, while update passes an already
constructed JavaScript array. The sampled cases are prepared overview
snapshots: they compare browser drawing cost, without a Shiny session or zoom
resampling. They do not permit data updates on a live sampled output.
`bench.updates` covers the real Shiny path, including server work and transfer.

Timing runs alternate operation order after warmup. For cross-case conclusions,
repeat the comparison with the case order reversed as well; results record that order. Profiled operations run
separately afterward; their durations are not used as latency evidence. Open
`.cpuprofile` files in Chrome DevTools' Performance panel and `.trace.json` files
in Perfetto. Captures continue through two animation frames after the operation,
so they include deferred layout and paint. CPU samples locate JavaScript work; browser
traces include layout and paint work. Profiles and result files remain local.

The cases deliberately change application choices:

- `stocks_gl` and `stocks_svg` retain all 505 series and 619,040 stock points.
- `stocks_sampled` uses a 200-point finite budget per series; this changes the
  overview approximation rather than making a full 619,040-point redraw faster.
- `stocks_grouped` puts 50 identically styled series in each trace, with a missing
  point between series and stock names in `customdata`. It retains all stock
  points but changes trace identity and individual series controls. The stock
  cases share one line color and width for a fair grouping comparison.
- `stocks_fixed` fixes the y range, sacrificing autorange as an explicit control.
- The power and building cases retain three sensor series. Sampled variants use
  a 2000-point finite budget; extra missing-value markers can exceed that budget.
- `power_regular` and `power_regular_steps` select the same longest constant-cadence
  interval in the meter recording (47,908 one-minute readings per series). The
  latter uses a date-string `x0` and millisecond `dx` instead of explicit timestamps.
  No readings are added or dropped within that interval. This is an unsampled
  application representation control, not a faster draw of the entire recording.

The Shiny benchmark also accepts `--case power_regular` and
`--case power_regular_steps`. They change the same first series and title;
`stocks` remains its default. Numeric epoch-date prototypes were rejected:
Plotly treats numeric dates as browser-local time and rounds fractional milliseconds,
so their apparent speedup did not preserve coordinate and zoom semantics.
Also check the recorded Plotly.js versions before comparing different benchmarks;
they must match.

Use sampling for long series, explicit updates for unchanged traces, and a date
representation suited to the application. Measure SVG versus WebGL on the actual
workload. Group traces only where separate trace identities and controls are not
needed. None of these choices is applied automatically by the package.


## First visible chart

    make bench-first-plot FIRST_PLOT_ARGS="--basic-bundle /path/to/plotly-basic.min.js --rounds 3 --output /tmp/first-plot.json"

Use a basic dist bundle matching `plotly.offline.get_plotlyjs_version()`; the
package validates its version and trace registrations. Install Playwright Chromium
with `make browsers`. The default uses downloaded Chrome for Testing in new
headless mode; `--headless-shell` selects the installed headless shell.

This measures the complete local Shiny startup path from navigation start to three
nonzero SVG bars followed by two animation frames. It includes HTTP downloads,
JavaScript parsing/evaluation, session initialization, figure serialization and
drawing. The checkpoint allows a rendering opportunity; it does not prove physical
screen presentation or GPU completion. Server imports, startup and compression are
outside the timer: compression is explicitly ready before navigation.

Each round compares full and basic bundles on the same three-bar figure, with
normal conditions and CDP emulation of 4x CPU slowdown, 80 ms latency, 1.6 Mbps
download and 0.75 Mbps upload. These are synthetic controls, not a calibrated
physical device or a WAN simulation. CDP HTTP throttling does not establish
WebSocket bandwidth limits. Bundle and throttle order alternate across rounds.
Use an idle machine and compare the same Chromium mode and dependency versions.

Every cold/warm pair has a new isolated browser context. Warm navigates away and
back while retaining its HTTP cache; it includes cached Shiny assets as well as
Plotly. The script checks resource timing to prove that Plotly transferred bytes
on cold visits and came from HTTP cache on warm visits. It also checks chart
values and page/output errors. HTTP cache warmth may also affect Chromium's code
cache, so it is not a pure network-only control.

Stdout reports medians; optional JSON includes every sample, versions, emulation
settings, DOM and rendering checkpoints, Plotly resource duration and encoded,
decoded and transfer sizes. Transfer size includes HTTP headers. The interval
from Plotly response end to chart DOM is also recorded: it includes remaining
browser/session/render work, not isolated parse time. Downloads and CPU work can
overlap; do not subtract resource duration from the total to infer CPU cost.
All servers, processes, contexts and temporary server caches are cleaned up.
Results remain local and should not be committed.

Add `--profiles /tmp/first-plot-profiles` to capture warm-cache startup under
throttling for both bundles. These are separate diagnostic runs after the timing
rounds; their timings never enter the reported medians. The browser first primes
its HTTP cache, then captures navigation through the chart rendering checkpoint
as a `.cpuprofile` and `.trace.json`. Open them in Chrome DevTools and Perfetto,
respectively. The optional result JSON records capture paths and a summary of
script evaluation by URL, layout/paint/parser events and CPU samples by source.
Diagnostic-only markers identify Shiny connection and the Plotly.newPlot call;
the result records its promise duration as well.
Some Chromium builds emit negative profiler sample deltas: the summary omits
them and records their count instead of reporting negative execution costs.
Trace categories overlap and sampled leaf times are not inclusive CPU costs;
use the timeline to distinguish execution from network waits. Profiling itself
adds overhead, so establish any speedup with separate unprofiled timing runs.


## Many-chart dashboards

    make bench-dashboard DASHBOARD_ARGS="--charts 20 --points 500 --repeats 5 --output /tmp/dashboard.json"

This runs real Shiny dashboards with four compact columns and two taller columns,
with offscreen deferral enabled and disabled. Charts contain identical-sized SVG
line traces and all draw once before timing begins. Timed refreshes change every
trace and title; the script waits for Plotly.react promises, then checks all drawn
values. It reports input-to-visible-chart time through two animation frames and
input-to-all-charts time, including server work and local WebSocket transport.
The checkpoint is not a physical screen-presentation or GPU-completion guarantee.

The default uses native idle scheduling and a 4x CPU emulation control. With
`--hold-idle`, background callbacks are deliberately held until flush: this
isolates foreground work rather than simulating normal browser idle behavior.
The all-chart measurement always flushes waiting redraws, so deferred work is
counted. First draws are never deferred. Four compact columns put the default
20 charts in the viewport; the tall layout has charts below the fold. Larger
chart counts can put charts below the fold in either layout; the actual visible
count is recorded per sample.

Two viewport changes count both the helper's observer resize requests and
Plotly's native resize handler invocations, then verify every graph's dimensions.
They also record relayout events and the number of native handlers still attached.
Request counts are not redraw counts: Plotly can debounce duplicate requests.
Resize elapsed times include a 150 ms settling period and are diagnostics, not
latency comparisons. The benchmark instruments event listeners and Plotly calls;
it is intended for comparisons with the same instrumentation.

One refresh warms each case before sampling. Repeat with `--reverse-order` to
check case-order effects; `--cpu 1` removes CPU emulation and `--headless-shell`
selects Playwright's older shell. Use the same browser mode and an idle machine.
Optional JSON records every sample, draw/resize counts and the case order. Results
are local artifacts and should remain outside Git. This benchmark does not time
cold asset downloads or server compression.

### Snapshot bursts

```sh
make bench-burst BURST_ARGS="--repeats 5 --output /tmp/burst.json"
make bench-burst BURST_ARGS="--hold-first-ms 250 --output /tmp/burst-held.json"
```

`bench/burst.py` sends five fixed full-figure snapshots, 20ms apart, over the actual
Shiny WebSocket to 20 charts with 500 SVG points each. It compares
`coalesce_renders=False/True` on four compact columns and on two tall columns with
offscreen deferral. The stacked cases hold idle callbacks until `flush()` to isolate
waiting-figure replacement; they are a controlled case, not native idle scheduling.
The first draw is warmed before measurement. Every sample verifies receipt of all
snapshots, final data in every graph and absence of browser errors, then measures
all-chart completion through flush and two animation frames. Case order reverses on
alternate repeats. Results include dependency/browser versions, raw samples, draw
counts, first/last-chart revisions and draws started after a newer snapshot arrived.

`--hold-first-ms` pauses the first redraw before delegating to Plotly to expose
asynchronous backpressure; that artificial wait is included in the timing. Draws of
older snapshots are only counted as obsolete at start if the raw socket listener had
already received a newer revision. CPU throttling (`--cpu 4` by default) is a synthetic
control, not a device prediction. Change `--charts`, `--points`, `--snapshots`,
`--interval-ms` and `--repeats` to match the workload. Keep generated results local.

### Normal reactive dashboards

```sh
make bench-reactive-dashboard REACTIVE_DASHBOARD_ARGS="--output /tmp/reactive-dashboard.json"
make bench-reactive-dashboard REACTIVE_DASHBOARD_ARGS="--cpu 1 --output /tmp/reactive-desktop.json"
```

`bench/reactive_dashboard.py` compares `coalesce_renders=False/True` through normal
`render_plotly` outputs, reactive invalidation and Shiny's own transport. It builds
12 charts, each with two NumPy-backed line traces of 1,000 points, from a shared
slider. No figure snapshots are injected and no browser drawing or idle callbacks
are held. Initial draws finish before measurement. Each sample uses a fresh page
and alternates case order between repeats.

Two input paths preserve Shiny's native rate policies:

- **Drag-style changes:** the slider widget is updated and emits ordinary change
  events eight times, 100ms apart. Shiny's default 250ms debounce remains active;
  this often collapses the entire interaction to one server render per chart.
  This is a repeatable widget-event sequence, not physical pointer automation.
- **Animation:** click Shiny's native slider Play button, with its 100ms interval
  and no looping. Its input binding sends continuous changes. Browser main-thread
  work can delay its timer, so the observed change/send timestamps are retained.

The benchmark checks every x/y value of every final trace, receipt of every built
figure, the final input revision and absence of browser errors. Completion includes
`flush()` and two animation frames. It records both total interaction duration and
time after the last actual slider change: a shorter interaction does not imply a
shorter settling delay. JSON includes raw input/send/receive/draw timelines, server
figure counts and construction time (excluding serialization), figure JSON payload
bytes (excluding WebSocket framing and other messages), and CDP main-thread task,
script and layout duration. Those browser durations include benchmark instrumentation;
they are not process CPU utilization.

Defaults are 20 repeats and synthetic 4x CPU throttling. Median and linearly
interpolated empirical p95 are reported; 20 samples still give only a rough tail
estimate. `--cpu 1` supplies the unthrottled control. Adjust `--charts`, `--traces`,
`--points`, `--steps`, `--interval-ms` and `--repeats` to match the app. The local
WebSocket transport adds no artificial network latency. Keep generated results local.
