"""A bounded, transactional cache shared by local compression workers."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

DIRECTORY_ENV = "SHINY_PLOTLY_CACHE_DIR"
DISABLE_ENV = "SHINY_PLOTLY_NO_CACHE"
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 32
MAX_BODY_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 4096


def cache_directory() -> Path | None:
    """Choose a user-local cache, with an override for shared deployment volumes."""
    if os.environ.get(DISABLE_ENV) or sys.platform == "emscripten":
        return None
    override = os.environ.get(DIRECTORY_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        parent = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        parent = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        parent = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return parent / "shiny-plotly"


def cache_key(digest: str, settings: str) -> str:
    """Identify the raw content, codec version, quality and deterministic settings."""
    return hashlib.sha256(f"v1:{digest}:{settings}".encode()).hexdigest()


def _sqlite():
    # Pyodide never uses this cache. Lazy import also permits native Python builds
    # without sqlite3 to keep serving compressed assets normally.
    try:
        import sqlite3
    except ImportError:
        return None
    return sqlite3


class CompressionCache:
    """SQLite transactions coordinate producers and commit complete encodings only.

    Reads never wait for a writer. A background producer waits at most five seconds
    for another process, then compresses locally if the cache is busy or unavailable.
    The database itself is capped at max_bytes; its rollback journal is transient.
    """

    def __init__(
        self, directory: Path, *, max_bytes: int = MAX_BYTES, max_entries: int = MAX_ENTRIES
    ) -> None:
        self.directory = directory
        self.path = directory / "compression-v1.sqlite3"
        self.max_bytes = max_bytes
        self.max_entries = max_entries

    def _lookup(self, connection, key: str) -> bytes | None:
        row = connection.execute(
            "SELECT body, checksum FROM encodings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        body, checksum = row
        if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != checksum:
            return None
        return body

    def peek(self, key: str) -> bytes | None:
        """Read a complete cached encoding without blocking startup on another worker."""
        sqlite = _sqlite()
        if sqlite is None:
            return None
        try:
            if not self.path.exists():
                return None
            with closing(sqlite.connect(self.path, timeout=0)) as connection:
                return self._lookup(connection, key)
        except (OSError, sqlite.Error):
            return None

    def get_or_create(self, key: str, produce: Callable[[], bytes]) -> bytes:
        """Produce once under a transaction; storage failures never discard the result."""
        sqlite = _sqlite()
        if sqlite is None:
            return produce()
        result = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(sqlite.connect(self.path, timeout=5)) as connection, connection:
                connection.execute(f"PRAGMA page_size = {PAGE_SIZE}")
                connection.execute(f"PRAGMA max_page_count = {self.max_bytes // PAGE_SIZE}")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS encodings "
                    "(key TEXT PRIMARY KEY, body BLOB NOT NULL, checksum TEXT NOT NULL)"
                )
                cached = self._lookup(connection, key)
                if cached is not None:
                    return cached
                result = produce()
                # Large custom bundles still work, but cannot crowd out the shared cache.
                budget = self.max_bytes // 2
                if len(result) > min(MAX_BODY_BYTES, budget) or self.max_entries < 1:
                    return result
                connection.execute("DELETE FROM encodings WHERE key = ?", (key,))
                while True:
                    count, size = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)), 0) FROM encodings"
                    ).fetchone()
                    if count < self.max_entries and size + len(result) <= budget:
                        break
                    connection.execute(
                        "DELETE FROM encodings WHERE rowid = "
                        "(SELECT rowid FROM encodings ORDER BY rowid LIMIT 1)"
                    )
                connection.execute(
                    "INSERT INTO encodings VALUES (?, ?, ?)",
                    (key, result, hashlib.sha256(result).hexdigest()),
                )
        except (OSError, sqlite.Error):
            # Includes disk-full, permissions, corruption and lock timeout. A result
            # already computed must not be compressed again just because storage failed.
            pass
        return result if result is not None else produce()
