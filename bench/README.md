# Performance workloads

Use an idle machine, the same Python/dependency versions and warmed operations for
comparisons. Server transform time and browser interaction latency are different
measurements. Results go to stdout; --output writes a local JSON artifact. Keep
downloaded fixtures and generated results outside Git.

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
