"""Reproducible server-side resampling timings; run ``make bench-resample``.

Use the same interpreter, dependencies and idle machine for comparisons. Figure/data
construction and payload hashing are outside timings. These are server operation
times, not browser interaction latency. Results go to stdout unless --output is given.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import time
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
from plotly.io.json import to_json_plotly

from shiny_plotly import render_plotly
from shiny_plotly._resample import resample_figure, sample, view_update


def measure(operation: Callable[[], Any], repeats: int) -> dict[str, float]:
    operation()  # warm the operation before measuring
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        operation()
        times.append((time.perf_counter() - start) * 1000)
    return {"median_ms": statistics.median(times), "min_ms": min(times)}


def digest(value: Any) -> str:
    encoded = to_json_plotly(value)
    assert isinstance(encoded, str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def workload(
    points: int, kind: str, budget: int, repeats: int, date_unit: str | None = None
) -> dict[str, Any]:
    x = np.arange(points, dtype=np.float64)
    y = np.sin(x / 1000)
    y[points // 2] = 20  # ensure the sample preserves a spike
    if kind == "sparse":
        y[::100_003] = np.nan
    elif kind == "dense":
        y[::3] = np.nan
    elif kind == "missing":
        y[:] = np.nan
    if date_unit is not None:
        dates = np.datetime64("2026-01-01", "us") + np.arange(points) * np.timedelta64(1, "s")
        x = dates.astype(f"datetime64[{date_unit}]", copy=False)
    row: dict[str, Any] = {"points": points, "kind": kind, "x_dtype": str(x.dtype)}
    row["sample"] = measure(lambda: sample(y, budget), repeats)
    row["sample_sha256"] = digest(sample(y, budget))

    figure = go.Figure(go.Scattergl(x=x, y=y, mode="lines"))
    renderer = render_plotly(resample=budget)
    loop = asyncio.new_event_loop()
    try:
        row["render"] = measure(
            lambda: loop.run_until_complete(renderer.transform(figure)), repeats
        )
        row["render_sha256"] = digest(loop.run_until_complete(renderer.transform(figure)))
    finally:
        loop.close()

    _, record = resample_figure({"data": [{"x": x, "y": y}]}, budget)
    series = record.series[0]
    span = (
        [float(series.coords[int(points * 0.01)]), float(series.coords[int(points * 0.99)])]
        if date_unit is not None
        else [points * 0.01, points * 0.99]
    )
    report = {"axes": {"x": span}}
    # Same complete view-update operation and payload, first forced direct, then with
    # normal lazy indexing. A dense/missing/small workload correctly stays direct.
    series.index_attempted = True
    row["view_direct"] = measure(lambda: view_update(record, report), repeats)
    expected = digest(view_update(record, report))
    series.index_attempted = False
    series.broad_views = 0
    cold = []
    for _ in range(3):
        start = time.perf_counter()
        result = view_update(record, report)
        cold.append((time.perf_counter() - start) * 1000)
        if date_unit is not None:
            if result is None:
                raise AssertionError("Date view returned no sample")
            kept = result["0"]["index_map"]
            if kept[0] != max(0, int(points * 0.01) - 1) or kept[-1] != min(
                points - 1, int(points * 0.99) + 1
            ):
                raise AssertionError("Date view did not preserve its window boundaries")
        if digest(result) != expected:
            raise AssertionError("Lazy index changed the direct sampler's payload")
    row["first_views_ms"] = cold
    row["view_steady"] = measure(lambda: view_update(record, report), repeats)
    row["view_sha256"] = digest(view_update(record, report))
    if row["view_sha256"] != expected:
        raise AssertionError("Indexed view changed the direct sampler's payload")
    index = series.extrema
    row["indexed"] = index is not None
    row["index_bytes"] = (
        0 if index is None else index.low.nbytes + index.high.nbytes + index.gaps.nbytes
    )
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", nargs="+", type=int, default=[1_000_000, 5_000_000])
    parser.add_argument("--budget", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--date-unit", choices=("us", "ns"), help="native datetime x control")
    parser.add_argument("--label", default="", help="Commit or comparison label for this run")
    parser.add_argument("--output", type=Path, help="Optional local JSON result file")
    args = parser.parse_args()
    if min(args.points) <= args.budget or args.budget < 10 or args.repeats < 1:
        parser.error(
            "points must exceed budget; budget must be at least 10; repeats must be positive"
        )
    result = {
        "label": args.label,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {name: version(name) for name in ("shiny-plotly", "numpy", "plotly", "shiny")},
        "budget": args.budget,
        "repeats": args.repeats,
        "workloads": [
            workload(n, kind, args.budget, args.repeats, args.date_unit)
            for n in args.points
            for kind in ("finite", "sparse", "dense", "missing")
        ],
    }
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
