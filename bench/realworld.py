"""Pinned real recordings and a many-trace control; see bench/README.md."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import platform
import statistics
import time
import urllib.request
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.io.json import to_json_plotly

import shiny_plotly
from shiny_plotly import render_plotly

SOURCES = {
    "power": (
        "https://raw.githubusercontent.com/predict-idlab/plotly-resampler/"
        "989043e53b905a472fc36db31d3f715c323a6585/examples/data/df_pc_test.parquet",
        "ce8da5651e4db292bb447fe9199b759472c5ebf3342de151887706f7e064320f",
    ),
    "building": (
        "https://raw.githubusercontent.com/predict-idlab/plotly-resampler/"
        "989043e53b905a472fc36db31d3f715c323a6585/examples/data/df_gusb.parquet",
        "c42f7ea2919e49652ef98dfe1f3427449052a5f9303389821360c56cee3f8abc",
    ),
    "stocks": (
        "https://raw.githubusercontent.com/plotly/datasets/"
        "0c447c47b757ad74edecab31f0d72f849d2e67c2/all_stocks_5yr.csv",
        "6aea253cd19de60b568143991aaf1fa482456565c389205658d236e595e716cf",
    ),
}


def source(cache: Path, name: str, fetch: bool) -> Path:
    url, expected = SOURCES[name]
    path = cache / (name + Path(url).suffix)
    if not path.exists():
        if not fetch:
            raise ValueError(f"Missing {path}; run with --fetch to download pinned fixtures")
        request = urllib.request.Request(
            url, headers={"User-Agent": "plotly-performance-benchmark/1.0"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Source checksum mismatch: {name}")
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Source checksum mismatch: {name}")
    return path


def prepare(cache: Path, name: str, fetch: bool) -> None:
    """Convert once with pyarrow; the portable cache contains no pickled objects."""
    frame = pd.read_parquet(source(cache, name, fetch))
    index = pd.DatetimeIndex(frame.index)
    # Epoch microseconds plus the timezone reconstruct the original timestamps,
    # including both sides of a DST transition. Numeric y arrays keep their dtype.
    dates = index.tz_convert("UTC").tz_localize(None).to_numpy(dtype="datetime64[us]")
    arrays = {"epoch_us": dates.astype(np.int64)}
    arrays.update({f"y{i}": frame[column].to_numpy() for i, column in enumerate(frame.columns)})
    np.savez(cache / (name + ".npz"), **arrays)
    metadata = {
        "timezone": str(index.tz),
        "columns": list(frame.columns),
        "source_sha256": SOURCES[name][1],
    }
    (cache / (name + ".json")).write_text(json.dumps(metadata))


def figure(cache: Path, name: str, *, fetch: bool = False, native: bool = False) -> go.Figure:
    if name == "stocks":
        frame = pd.read_csv(source(cache, name, fetch))
        frame["date"] = pd.to_datetime(frame["date"])
        traces = [
            go.Scattergl(x=rows["date"], y=rows["close"], name=symbol, mode="lines")
            for symbol, rows in frame.groupby("Name", sort=True)
        ]
    else:
        if not (cache / (name + ".npz")).exists() or not (cache / (name + ".json")).exists():
            prepare(cache, name, fetch)
        metadata = json.loads((cache / (name + ".json")).read_text())
        if metadata["source_sha256"] != SOURCES[name][1]:
            raise ValueError(f"Prepared source checksum mismatch: {name}")
        with np.load(cache / (name + ".npz"), allow_pickle=False) as data:
            dates = pd.to_datetime(data["epoch_us"], unit="us", utc=True).tz_convert(
                metadata["timezone"]
            )
            x = (
                dates.tz_localize(None).to_numpy(dtype="datetime64[us]")
                if native
                else dates.to_numpy(dtype=object)
            )
            traces = [
                go.Scattergl(x=x, y=data[f"y{i}"], name=column, mode="lines")
                for i, column in reversed(list(enumerate(metadata["columns"])))
            ]
    return go.Figure(traces).update_layout(
        height=450, uirevision=name, title=name, showlegend=name != "stocks"
    )


async def measure(fig: go.Figure, *, sampled: bool, repeats: int) -> dict[str, Any]:
    renderer = render_plotly(resample=2000 if sampled else None)
    payload = await renderer.transform(fig)
    durations = []
    for _ in range(repeats):
        payload = None  # free the previous result outside the timed region
        gc.collect()
        start = time.perf_counter()
        payload = await renderer.transform(fig)
        durations.append((time.perf_counter() - start) * 1000)
    encoded = to_json_plotly(payload)
    assert isinstance(encoded, str)
    traces = cast(Any, fig).data
    return {
        "traces": len(traces),
        "points": sum(len(trace.x) for trace in traces),
        "median_ms": statistics.median(durations),
        "min_ms": min(durations),
        "payload_bytes": len(encoded.encode()),
        "payload_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "dependencies": {
            name: version(name) for name in ("numpy", "pandas", "plotly", "shiny", "shiny-plotly")
        },
        "shiny_plotly_source": shiny_plotly.__file__,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/shiny-plotly-realworld"))
    parser.add_argument("--fetch", action="store_true", help="download checksum-pinned fixtures")
    parser.add_argument(
        "--prepare", action="store_true", help="convert Parquet once; needs pyarrow"
    )
    parser.add_argument("--cases", nargs="+", choices=SOURCES, default=list(SOURCES))
    parser.add_argument("--native-dates", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.prepare and args.output:
        parser.error("--output is for measured results; omit it when preparing fixtures")
    results: dict[str, Any] = {
        "environment": environment(),
        "native_dates": args.native_dates,
        "repeats": args.repeats,
        "sources": SOURCES,
        "workloads": {},
    }
    for name in args.cases:
        if args.prepare:
            if name != "stocks":
                prepare(args.cache, name, args.fetch)
            else:
                source(args.cache, name, args.fetch)
            continue
        fig = figure(args.cache, name, fetch=args.fetch, native=args.native_dates)
        results["workloads"][name] = asyncio.run(
            measure(fig, sampled=name != "stocks", repeats=args.repeats)
        )
    if args.prepare:
        print(json.dumps({"prepared": args.cases, "cache": str(args.cache)}, indent=2))
        return
    text = json.dumps(results, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
