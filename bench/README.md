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
