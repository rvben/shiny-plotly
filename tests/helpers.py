"""Test helper: run a coroutine from a synchronous test."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    """
    The coroutine on its own loop in a worker thread. The main thread cannot run one
    while the browser tests' session-wide playwright fixture is alive (its sync API
    keeps a loop running there), and nothing awaited here needs that loop anyway.

    The caller's context goes with it, so a ``session_context`` around the call is the
    session the coroutine sees; a plain thread would leave it looking like no session.
    """
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coro).result()
