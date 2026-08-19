"""Test helper: pull the arguments of the emitted ``Plotly.newPlot(...)`` call out of HTML."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from htmltools import TagList


@dataclass
class NewPlotCall:
    div_id: str
    data: list[Any]
    layout: dict[str, Any]
    config: dict[str, Any]


def render_html(x: Any) -> str:
    return str(TagList(x))


def parse_newplot(html: str) -> NewPlotCall:
    """Decode the four JSON arguments of the single ``Plotly.newPlot(`` call in ``html``."""
    calls = [m.end() for m in re.finditer(r"Plotly\.newPlot\(", html)]
    assert len(calls) == 1, f"expected exactly one Plotly.newPlot call, found {len(calls)}"
    pos = calls[0]
    decoder = json.JSONDecoder()
    args: list[Any] = []
    for _ in range(4):
        pos = _skip_separators(html, pos)
        value, pos = decoder.raw_decode(html, pos)
        args.append(value)
    div_id, data, layout, config = args
    return NewPlotCall(div_id=div_id, data=data, layout=layout, config=config)


def _skip_separators(s: str, pos: int) -> int:
    while pos < len(s) and (s[pos].isspace() or s[pos] == ","):
        pos += 1
    return pos
