"""Owned Plotly snapshots without reconstructing immutable dates point by point."""

from __future__ import annotations

import datetime
import sys
from copy import deepcopy
from typing import Any, cast

from plotly.basedatatypes import BasePlotlyType


def _date_arrays(value: Any, memo: dict[int, Any], seen: set[int], zones: dict[int, bool]) -> None:
    if id(value) in seen or id(value) in memo:
        return
    seen.add(id(value))
    numpy = sys.modules.get("numpy")
    is_array = numpy is not None and type(value) is numpy.ndarray and value.dtype.kind == "O"
    if is_array or type(value) is list:
        pandas = sys.modules.get("pandas")
        types = (datetime.date, datetime.datetime)
        if numpy is not None:
            types += (numpy.datetime64,)
        if pandas is not None:
            types += (pandas.Timestamp,)
        timestamps_only = pandas is not None
        for item in cast(Any, value).flat if is_array else value:
            timestamps_only = (
                pandas is not None and timestamps_only and type(item) is pandas.Timestamp
            )
            if type(item) not in types:
                break
            if getattr(item, "__dict__", None):
                break  # Timestamp instances can carry caller-added mutable attributes
            if pandas is not None and type(item) is pandas.Timestamp and not 1 <= item.year <= 9999:
                break  # pandas' object boxing can wrap years outside Python's date range
            # A datetime can carry a mutable user-defined tzinfo. Keep deepcopy
            # for it, and for date subclasses with additional mutable state.
            if isinstance(item, datetime.datetime) and item.tzinfo is not None:
                zone = item.tzinfo
                if id(zone) not in zones:
                    # pytz and cached ZoneInfo preserve identity through deepcopy;
                    # mutable custom zones do not. Match the serializer's isolation.
                    zones[id(zone)] = type(zone) is datetime.timezone or deepcopy(zone) is zone
                if not zones[id(zone)]:
                    break
        else:
            owned = cast(Any, value).copy(order="K") if is_array else value.copy()
            if pandas is not None and timestamps_only:
                try:
                    dates = pandas.DatetimeIndex(
                        cast(Any, value).ravel() if is_array else value
                    ).to_numpy(dtype=object, copy=True)
                except (TypeError, ValueError, OverflowError):
                    return  # mixed zones or unrepresentable dates retain deepcopy
                if is_array:
                    cast(Any, owned).flat[:] = dates
                else:
                    owned[:] = dates
            elif pandas is not None:
                for index, item in enumerate(cast(Any, value).flat if is_array else value):
                    if type(item) is pandas.Timestamp:
                        # Timestamp date values are fixed but instances can acquire
                        # attributes later. A C-level clone avoids deepcopy's Python
                        # reconstruction while isolating these instance dictionaries.
                        try:
                            cloned = item.replace()
                        except (TypeError, ValueError, OverflowError):
                            cloned = deepcopy(item)
                        if is_array:
                            cast(Any, owned).flat[index] = cloned
                        else:
                            owned[index] = cloned
            memo[id(value)] = owned
            return
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        # Ordinary numeric/string lists dominate many apps. If their first item
        # is scalar, skip discovery altogether; deepcopy still owns every value.
        if (
            value
            and not isinstance(value[0], (dict, list, tuple))
            and not (numpy is not None and isinstance(value[0], numpy.ndarray))
        ):
            return
        children = value
    else:
        return
    for item in children:
        # Scalars need no traversal or seen entry; leave their copying to deepcopy.
        if isinstance(item, (dict, list, tuple)) or (
            numpy is not None and isinstance(item, numpy.ndarray)
        ):
            _date_arrays(item, memo, seen, zones)


def snapshot(component: BasePlotlyType) -> dict[str, Any]:
    # Plotly's public component serializer is deepcopy(_props), but does not take
    # a memo. Read that storage only for its standard serializer; never write it.
    # A custom serializer or a future storage shape uses the public method instead.
    props = getattr(component, "_props", None)
    if (
        not isinstance(props, dict)
        or type(component).to_plotly_json is not BasePlotlyType.to_plotly_json
    ):
        return deepcopy(component.to_plotly_json())
    # Each public component serializer takes its own copy. Do not introduce new
    # aliases between traces, layout or frames (encoding mutates these snapshots).
    memo: dict[int, Any] = {}
    _date_arrays(props, memo, set(), {})
    return deepcopy(props, memo)
