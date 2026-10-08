"""Choose a process-wide plotly.js bundle and validate the traces sent to it."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from plotly.io.json import to_json_plotly
from plotly.offline import get_plotlyjs_version

__all__ = (
    "ENV_VAR",
    "PlotlyBundle",
    "check_figure",
    "check_trace_types",
    "selected_bundle",
    "use_plotly_bundle",
)

ENV_VAR = "SHINY_PLOTLY_BUNDLE"
_BANNER = re.compile(r"plotly\.js(?: \((?P<variant>[^)]*)\))? v(?P<version>\d+\.\d+\.\d+\S*)")
# Start with a literal so re can skip directly to registration sites. A quoted
# key still matches from inside its opening quote, including in custom builds.
_TRACE = re.compile(
    rb"""moduleType["']?\s*:\s*["']trace["']\s*,\s*["']?name["']?\s*:\s*["'](\w+)["']"""
)
_BANNER_BYTES = 1024
_DEFAULT_TRACE = "scatter"
_DIST_VARIANTS = frozenset(
    {"full", "basic", "cartesian", "finance", "geo", "gl2d", "gl3d", "mapbox", "strict"}
)


@dataclass(frozen=True)
class PlotlyBundle:
    """Validated metadata and the exact bytes that will be served."""

    path: Path
    variant: str
    plotlyjs_version: str
    trace_types: frozenset[str]
    digest: str
    content: bytes = field(repr=False)

    @property
    def local_version(self) -> str:
        """A dependency version that identifies both the variant and its content."""
        variant = re.sub(r"[^a-z0-9]+", ".", self.variant.lower()).strip(".") or "custom"
        return f"{variant}.{self.digest}"


def download_url(variant: str, version: str) -> str:
    """The published minified bundle of this version (custom builds must be rebuilt)."""
    suffix = "" if variant == "full" else f"-{variant}"
    return (
        f"https://cdn.jsdelivr.net/npm/plotly.js{suffix}-dist-min@{version}/plotly{suffix}.min.js"
    )


def read_bundle(path: str | os.PathLike[str]) -> PlotlyBundle:
    """Read once, validating the same snapshot that will be copied and served."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    banner = _BANNER.search(raw[:_BANNER_BYTES].decode("utf-8", "replace"))
    if banner is None:
        raise ValueError(f"{path} is not a plotly.js dist bundle: it has no plotly.js banner")
    variant = (banner["variant"] or "full").split(" - ")[0].strip() or "full"
    version = banner["version"]
    expected = get_plotlyjs_version()
    if version != expected:
        remedy = (
            f"Use the {variant} bundle of that version: {download_url(variant, expected)}"
            if variant in _DIST_VARIANTS
            else f"Rebuild this custom bundle with plotly.js {expected}"
        )
        raise ValueError(
            f"{path} is plotly.js {version}, but the installed plotly builds figures for "
            f"plotly.js {expected}. {remedy}"
        )
    trace_types = frozenset(m.decode() for m in _TRACE.findall(raw))
    if not trace_types:
        raise ValueError(f"{path} registers no trace types that shiny-plotly can find")
    return PlotlyBundle(path, variant, version, trace_types, hashlib.sha256(raw).hexdigest(), raw)


_lock = threading.Lock()
_chosen: PlotlyBundle | None = None
_settled = False
_selected: tuple[PlotlyBundle, Path] | None = None
_directory: tempfile.TemporaryDirectory[str] | None = None


def use_plotly_bundle(path: str | os.PathLike[str]) -> None:
    """
    Serve a partial or custom plotly.js dist bundle instead of plotly's full bundle.

    Call before building the first plotly output, fragment or explicit plotly_js()
    dependency. The choice is per process and cannot change afterwards; choosing the
    same content again is harmless. A call overrides SHINY_PLOTLY_BUNDLE.

    The file must target exactly plotly.offline.get_plotlyjs_version(). It is read once,
    validated, and served from a private snapshot under a content-keyed URL. Figures,
    animation frames, added traces and trace-type updates are checked against its types.
    """
    global _chosen
    bundle = read_bundle(path)
    with _lock:
        if _settled:
            current = None if _selected is None else _selected[0]
            if current is None or current.content != bundle.content:
                raise RuntimeError(
                    "use_plotly_bundle() must be called before the first plotly output or "
                    "figure is built; the plotly.js bundle is already chosen for this process"
                )
            return
        _chosen = bundle


def selection_settled() -> bool:
    """Whether a dependency has fixed the choice; inspecting this never selects one."""
    with _lock:
        return _settled


def selected_bundle() -> tuple[PlotlyBundle, Path] | None:
    """Settle the choice and privately copy its validated bytes, or use the full bundle."""
    global _chosen, _settled, _selected, _directory
    with _lock:
        if not _settled:
            bundle = _chosen
            if bundle is None and os.environ.get(ENV_VAR):
                bundle = read_bundle(os.environ[ENV_VAR])
            if bundle is not None:
                directory = tempfile.TemporaryDirectory(prefix="shiny-plotly-bundle-")
                try:
                    path = Path(directory.name)
                    (path / "plotly.min.js").write_bytes(bundle.content)
                except BaseException:
                    directory.cleanup()
                    raise
                atexit.register(directory.cleanup)
                _directory = directory
                _selected = (bundle, path)
            _chosen = None
            _settled = True
        return _selected


def check_trace_types(types: Iterable[str]) -> None:
    """Refuse a trace type missing from the selected bundle."""
    selected = selected_bundle()
    if selected is None:
        return
    bundle = selected[0]
    missing = sorted(set(types) - bundle.trace_types)
    if missing:
        raise ValueError(
            f"trace type {', '.join(map(repr, missing))} is not in the {bundle.variant} "
            f"plotly.js bundle ({bundle.path}), which would draw nothing for it; that bundle "
            f"has {', '.join(sorted(bundle.trace_types))}"
        )


def trace_types(
    traces: Iterable[Mapping[str, Any]], default: str | None = _DEFAULT_TRACE
) -> Iterable[str]:
    """Types in trace dicts; omitted frame types inherit the trace they animate."""
    for trace in traces:
        kind = trace.get("type")
        if isinstance(kind, str):
            yield kind
        elif "type" in trace or default is not None:
            yield _DEFAULT_TRACE


def check_figure(fig_dict: Mapping[str, Any]) -> None:
    """Check figure data and frames, including explicit resets to the default type."""
    if selected_bundle() is None:
        return
    check_trace_types(trace_types(fig_dict.get("data") or ()))
    for frame in fig_dict.get("frames") or ():
        check_trace_types(trace_types(frame.get("data") or (), default=None))


def check_style(style: Mapping[str, Any] | None) -> None:
    """Check serialized restyle values; null resets type to scatter, omission preserves it."""
    if style is None or "type" not in style:
        return
    if selected_bundle() is None:
        return
    kind = json.loads(cast(str, to_json_plotly(style["type"])))
    kinds = kind if isinstance(kind, list) else [kind]
    # Invalid non-string values also coerce to the default in plotly.js.
    check_trace_types(k if isinstance(k, str) else _DEFAULT_TRACE for k in kinds)


def _reset() -> None:
    """Reset selection and clean up its private directory for isolated tests."""
    global _chosen, _settled, _selected, _directory
    with _lock:
        if _directory is not None:
            atexit.unregister(_directory.cleanup)
            _directory.cleanup()
        _chosen, _settled, _selected, _directory = None, False, None, None
