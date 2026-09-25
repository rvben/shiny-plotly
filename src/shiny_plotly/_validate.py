"""Argument checks shared by the renderer and the in-place updates."""

from __future__ import annotations

import operator
from typing import Any


def as_int(value: Any) -> int:
    """
    ``value`` as a Python int, for anything integer-like: ``int`` and numpy's integers.

    Raises TypeError for anything else. ``bool`` is refused by name: it is an int in Python
    but serializes to a JSON ``true``, which plotly.js does not read as a number. numpy's
    bool is refused by its dtype, since numpy before 2.3 still lets it pass as an index,
    with only a DeprecationWarning, as 0 or 1. A float, even a whole one, is refused as
    well, the way ``range()`` and list indexing refuse it.
    """
    if isinstance(value, bool) or getattr(getattr(value, "dtype", None), "kind", None) == "b":
        raise TypeError(f"expected an integer, got {value!r}")
    return operator.index(value)


def as_positive_int(value: Any, name: str) -> int:
    """``value`` as a Python int of at least 1; ValueError naming ``name`` otherwise."""
    try:
        number = as_int(value)
    except TypeError:
        number = 0
    if number < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return number
