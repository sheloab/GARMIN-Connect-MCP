import threading
import time

from mcp_garmin_connect.cache import TTLCache


def test_ttl_cache_reuses_value() -> None:
    cache = TTLCache(ttl_seconds=60)
    calls = 0

    def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert cache.get_or_set("x", factory) == 1
    assert cache.get_or_set("x", factory) == 1
    assert calls == 1


def test_ttl_cache_clear() -> None:
    cache = TTLCache(ttl_seconds=60)
    cache.get_or_set("x", lambda: 1)
    cache.clear()
    assert cache.get_or_set("x", lambda: 2) == 2


def test_ttl_cache_delete() -> None:
    cache = TTLCache(ttl_seconds=60)
    assert cache.get_or_set("x", lambda: 1) == 1
    cache.delete("x")
    assert cache.get_or_set("x", lambda: 2) == 2


def test_ttl_cache_single_flight() -> None:
    cache = TTLCache(ttl_seconds=60)
    calls = 0
    calls_lock = threading.Lock()

    def factory() -> int:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return 42

    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_or_set("k", factory)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert results == [42] * 8


def test_ttl_cache_factory_errors_are_not_cached() -> None:
    cache = TTLCache(ttl_seconds=60)
    attempts = 0

    def failing_factory() -> int:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    for _ in range(2):
        try:
            cache.get_or_set("k", failing_factory)
        except RuntimeError:
            pass

    assert attempts == 2
