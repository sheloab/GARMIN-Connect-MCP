from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Generic, TypeVar

T = TypeVar("T")

DEFAULT_MAX_ENTRIES = 512


@dataclass
class _Entry(Generic[T]):
    expires_at: float
    value: T


class TTLCache:
    """Thread-safe TTL cache with per-key single-flight factories.

    Concurrent callers that request the same missing key wait for the first
    factory to finish instead of re-running it (no cache stampede), and failed
    factories are never cached so the next caller can retry.
    """

    def __init__(self, ttl_seconds: int, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[str, _Entry[object]] = OrderedDict()
        self._key_locks: dict[str, Lock] = {}
        self._lock = RLock()

    def get_or_set(self, key: str, factory: Callable[[], T]) -> T:
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry.expires_at > time.time():
                self._entries.move_to_end(key)
                return entry.value  # type: ignore[return-value]
            key_lock = self._key_locks.setdefault(key, Lock())

        with key_lock:
            try:
                with self._lock:
                    entry = self._entries.get(key)
                    if entry and entry.expires_at > time.time():
                        return entry.value  # type: ignore[return-value]

                value = factory()

                with self._lock:
                    self._entries[key] = _Entry(
                        expires_at=time.time() + self.ttl_seconds,
                        value=value,
                    )
                    self._entries.move_to_end(key)
                    self._prune()
                return value
            finally:
                with self._lock:
                    self._key_locks.pop(key, None)

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def _prune(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
