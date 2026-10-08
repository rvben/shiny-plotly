"""Persistent encodings: reuse, coordination, integrity, bounded storage and fallback."""

import gzip
import multiprocessing
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import brotli
import pytest

from shiny_plotly import _cache, _serve
from shiny_plotly._cache import CompressionCache, cache_key


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    directory = tmp_path / "cache"
    monkeypatch.delenv(_cache.DISABLE_ENV, raising=False)
    monkeypatch.setenv(_cache.DIRECTORY_ENV, str(directory))
    return directory


def asset(tmp_path, name="plotly.min.js"):
    path = tmp_path / name
    path.write_bytes(b"!function(){window.Plotly={}}();\n" * 100)
    return path


def ready_bundle(path):
    bundle = _serve.CompressedBundle(path)
    bundle.start()
    assert bundle.wait(timeout=10)
    return bundle


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_default_cache_is_user_local(platform, tmp_path, monkeypatch):
    monkeypatch.delenv(_cache.DISABLE_ENV, raising=False)
    monkeypatch.delenv(_cache.DIRECTORY_ENV, raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(_cache, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    expected = {"darwin": "Library/Caches", "win32": "AppData/Local", "linux": ".cache"}
    assert _cache.cache_directory() == tmp_path / expected[platform] / "shiny-plotly"


@pytest.mark.parametrize("platform,var", [("linux", "XDG_CACHE_HOME"), ("win32", "LOCALAPPDATA")])
def test_os_cache_location_is_respected(platform, var, tmp_path, monkeypatch):
    monkeypatch.delenv(_cache.DISABLE_ENV, raising=False)
    monkeypatch.delenv(_cache.DIRECTORY_ENV, raising=False)
    monkeypatch.setattr(_cache, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv(var, str(tmp_path))
    assert _cache.cache_directory() == tmp_path / "shiny-plotly"


def test_override_and_disable(cache_dir, monkeypatch):
    assert _cache.cache_directory() == cache_dir
    monkeypatch.setenv(_cache.DISABLE_ENV, "1")
    assert _cache.cache_directory() is None
    monkeypatch.delenv(_cache.DISABLE_ENV)
    monkeypatch.setattr(_cache, "sys", SimpleNamespace(platform="emscripten"))
    assert _cache.cache_directory() is None


def test_key_includes_content_and_settings():
    assert cache_key("a", "br:q9:v1") == cache_key("a", "br:q9:v1")
    assert (
        len(
            {
                cache_key("a", "br:q9:v1"),
                cache_key("b", "br:q9:v1"),
                cache_key("a", "br:q9:v2"),
                cache_key("a", "br:q10:v1"),
                cache_key("a", "gzip:q9:v1"),
            }
        )
        == 5
    )


def test_warm_start_serves_cached_encodings_immediately_without_a_thread(
    cache_dir, tmp_path, monkeypatch
):
    path = asset(tmp_path)
    cold = ready_bundle(path)
    assert cold._raw is None
    assert gzip.decompress(cold.encodings["gzip"]) == path.read_bytes()
    assert brotli.decompress(cold.encodings["br"]) == path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("warm start must not compress or launch a thread")

    monkeypatch.setattr(_serve.gzip, "compress", forbidden)
    monkeypatch.setattr(_serve.brotli, "compress", forbidden)
    monkeypatch.setattr(_serve.threading, "Thread", forbidden)
    warm = _serve.CompressedBundle(path)
    warm.start()
    warm.start()
    assert warm.ready
    assert warm._raw is None
    assert warm.encodings == cold.encodings
    response = _serve.response_for(warm, accept_encoding="br", if_none_match=None)
    assert response.headers["content-encoding"] == "br"
    assert response.headers["cache-control"] == _serve.CACHE_CONTROL


def test_private_copy_location_does_not_change_cache_identity(cache_dir, tmp_path):
    a = asset(tmp_path, "a.js")
    b = asset(tmp_path, "b.js")
    os.utime(b, ns=(1, 1))
    first = ready_bundle(a)
    second = ready_bundle(b)
    assert second.ready
    assert second.etag_base == first.etag_base
    assert second._cache_keys == first._cache_keys
    assert second.encodings == first.encodings
    assert (
        _serve.response_for(
            second, accept_encoding="gzip", if_none_match=first.etag("gzip")
        ).status_code
        == 304
    )


def test_same_size_same_timestamp_different_content_cannot_reuse_encodings(cache_dir, tmp_path):
    path = asset(tmp_path)
    first = ready_bundle(path)
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"Plotly", b"NewOne"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = ready_bundle(path)
    assert second._cache_keys != first._cache_keys
    assert second.etag_base != first.etag_base
    assert gzip.decompress(second.encodings["gzip"]) == path.read_bytes()


def test_partially_cached_bundle_only_produces_missing_encoding(cache_dir, tmp_path, monkeypatch):
    path = asset(tmp_path)
    first = ready_bundle(path)
    with closing(sqlite3.connect(CompressionCache(cache_dir).path)) as connection, connection:
        connection.execute("DELETE FROM encodings WHERE key = ?", (first._cache_keys["gzip"],))
    monkeypatch.setattr(
        _serve.brotli, "compress", lambda *a, **kw: pytest.fail("br already cached")
    )
    second = ready_bundle(path)
    assert second.encodings == first.encodings


def test_no_brotli_warning_survives_a_warm_cache(cache_dir, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(_serve, "brotli", None)
    path = asset(tmp_path)
    ready_bundle(path)
    caplog.clear()
    second = _serve.CompressedBundle(path)
    second.start()
    assert second.ready
    assert len(caplog.records) == 1
    assert "Install brotli" in caplog.text


def test_bad_cached_body_is_rejected_and_rebuilt(cache_dir):
    cache = CompressionCache(cache_dir)
    assert cache.get_or_create("key", lambda: b"valid") == b"valid"
    assert cache.get_or_create("key", lambda: pytest.fail("already cached")) == b"valid"
    with closing(sqlite3.connect(cache.path)) as connection, connection:
        connection.execute("UPDATE encodings SET body = ?", (b"corrupt",))
    assert cache.peek("key") is None
    assert cache.get_or_create("key", lambda: b"rebuilt") == b"rebuilt"
    assert cache.peek("key") == b"rebuilt"
    with closing(sqlite3.connect(cache.path)) as connection, connection:
        connection.execute("UPDATE encodings SET body = 'not a blob'")
    assert cache.peek("key") is None


def test_oldest_entries_are_evicted_by_count_and_bytes(cache_dir):
    cache = CompressionCache(cache_dir, max_bytes=32768, max_entries=2)
    for name in ("a", "b", "c"):
        assert cache.get_or_create(name, lambda: b"x" * 100) == b"x" * 100
    assert cache.peek("a") is None
    assert cache.peek("b") == b"x" * 100
    cache.get_or_create("d", lambda: b"x" * 16000)
    assert cache.peek("b") is None
    assert cache.peek("c") is not None
    cache.get_or_create("e", lambda: b"x" * 16000)
    assert cache.peek("d") is None
    assert cache.peek("e") == b"x" * 16000
    assert cache.path.stat().st_size <= cache.max_bytes


@pytest.mark.parametrize("max_entries,body", [(0, b"small"), (32, b"x" * 20000)])
def test_entries_that_do_not_fit_are_served_without_caching(cache_dir, max_entries, body):
    cache = CompressionCache(cache_dir, max_bytes=32768, max_entries=max_entries)
    assert cache.get_or_create("large", lambda: body) == body
    assert cache.peek("large") is None


def test_database_size_limit_does_not_recompress_after_failed_write(cache_dir):
    cache = CompressionCache(cache_dir, max_bytes=12288)
    calls = []

    def produce():
        calls.append(1)
        return b"x" * 6000

    assert cache.get_or_create("key", produce) == b"x" * 6000
    assert calls == [1]
    assert cache.peek("key") is None
    assert cache.path.stat().st_size <= 12288


def test_unavailable_or_corrupt_storage_falls_back(cache_dir):
    cache_dir.write_text("not a directory")
    cache = CompressionCache(cache_dir)
    assert cache.peek("key") is None
    assert cache.get_or_create("key", lambda: b"fallback") == b"fallback"
    cache_dir.unlink()
    cache_dir.mkdir()
    cache.path.write_bytes(b"not a database")
    assert cache.peek("key") is None
    assert cache.get_or_create("key", lambda: b"fallback") == b"fallback"


def test_sqlite_is_optional(cache_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    cache = CompressionCache(cache_dir)
    assert cache.peek("key") is None
    assert cache.get_or_create("key", lambda: b"fallback") == b"fallback"


def test_busy_cache_never_blocks_the_startup_reader(cache_dir):
    cache = CompressionCache(cache_dir)
    cache.get_or_create("key", lambda: b"body")
    with closing(sqlite3.connect(cache.path)) as connection, connection:
        connection.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        assert cache.peek("key") is None
        assert time.monotonic() - start < 1


def test_lock_timeout_falls_back_without_waiting_indefinitely(cache_dir, monkeypatch):
    cache = CompressionCache(cache_dir)
    cache.get_or_create("key", lambda: b"body")
    connect = sqlite3.connect
    # Exercise the same busy-handler path with a short test timeout.
    monkeypatch.setattr(sqlite3, "connect", lambda path, timeout: connect(path, timeout=0.01))
    with closing(connect(cache.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert cache.get_or_create("other", lambda: b"fallback") == b"fallback"
    assert cache.peek("other") is None


def test_producer_errors_roll_back_and_release_the_lock(cache_dir):
    cache = CompressionCache(cache_dir)

    def fail():
        raise ValueError("compression failed")

    with pytest.raises(ValueError, match="compression failed"):
        cache.get_or_create("key", fail)
    assert cache.get_or_create("key", lambda: b"recovered") == b"recovered"


def _worker(directory, barrier, results):
    cache = CompressionCache(Path(directory))

    def produce():
        with (Path(directory) / "producers.txt").open("a") as log:
            log.write("compressed\n")
        time.sleep(0.15)
        return b"complete encoding" * 100

    barrier.wait(timeout=10)
    results.put(cache.get_or_create("shared", produce))


def test_simultaneous_processes_share_one_producer(cache_dir):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(target=_worker, args=(str(cache_dir), barrier, results)) for _ in range(3)
    ]
    try:
        for process in processes:
            process.start()
        assert [results.get(timeout=15) for _ in processes] == [b"complete encoding" * 100] * 3
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()
        results.join_thread()
    assert (cache_dir / "producers.txt").read_text().splitlines() == ["compressed"]
    assert CompressionCache(cache_dir).peek("shared") == b"complete encoding" * 100


def test_codec_version_changes_key_and_etag(cache_dir, tmp_path, monkeypatch):
    path = asset(tmp_path)
    first = _serve.CompressedBundle(path)
    monkeypatch.setattr(_serve.brotli, "__version__", "different-codec-version")
    second = _serve.CompressedBundle(path)
    assert second._cache_keys["br"] != first._cache_keys["br"]
    assert second.etag("br") != first.etag("br")
    assert second.etag(None) == first.etag(None)
    assert second.etag("gzip") == first.etag("gzip")
