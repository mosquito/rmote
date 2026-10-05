"""Explicit in-memory caching of arbitrary tool results.

Values are retained as supplied, without deepcopy, freezing or collector
validation. Their owner is responsible for mutations. Persistence is explicit,
and each backend defines which value representations it supports.
"""

import math
import time
from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Generic, TypeVar, cast

from .backends import CacheBackend, CacheEntry, JSONCacheDir, SQLiteCache

__all__ = ["Cache", "CacheBackend", "CacheEntry", "JSONCacheDir", "SQLiteCache"]

T = TypeVar("T")


class Cache(Mapping[str, T], Generic[T]):
    """Store named results without knowing which tool produced them.

    Optional ``versions`` declare expected versions and keys to check for
    freshness. Other keys remain valid and default to version 1. The first
    load/save binds this instance to one namespace (for example an SSH host).
    Read-only key snapshots share the original values; they do not promise
    deep immutability. Use ``freeze`` explicitly when that is required.
    """

    def __init__(self, *, versions: Mapping[str, int] | None = None) -> None:
        self.versions = MappingProxyType(dict(versions or {}))
        for key, version in self.versions.items():
            self.validate_key(key)
            if type(version) is not int or version < 1:
                raise ValueError("version must be a positive integer")
        self.entries: Mapping[str, CacheEntry[T]] = MappingProxyType({})
        self._data: Mapping[str, T] = MappingProxyType({})
        self.namespace: str | None = None

    @staticmethod
    def validate_key(key: str) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("cache key must be a nonempty string")

    def select(self, keys: Iterable[str] | None) -> tuple[str, ...]:
        if isinstance(keys, str):
            raise ValueError("keys must be an iterable of keys, not a string")
        selected = tuple(dict.fromkeys((*self.versions, *self.entries) if keys is None else keys))
        for key in selected:
            self.validate_key(key)
        return selected

    @property
    def data(self) -> Mapping[str, T]:
        """Return the current key snapshot in O(1), sharing retained values."""
        return self._data

    def __getitem__(self, key: str) -> T:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def update(self, values: Mapping[str, T], *, collected_at: float | None = None) -> None:
        """Replace complete branches, preserving newer observations.

        Values are retained by reference. Invalid keys or timestamps leave the
        entire cache unchanged. No remote calls or storage I/O are performed.
        """
        timestamp = time.time() if collected_at is None else collected_at
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("collected_at must be finite and non-negative")
        entries = {}
        for key, value in values.items():
            self.validate_key(key)
            entries[key] = CacheEntry(value, timestamp, self.versions.get(key, 1))
        self.merge(entries)

    def merge(self, entries: Mapping[str, CacheEntry[T]]) -> None:
        merged = dict(self.entries)
        for key, entry in entries.items():
            self.validate_key(key)
            previous = merged.get(key)
            if previous is None or previous.collected_at <= entry.collected_at:
                merged[key] = entry
        self.entries = MappingProxyType(merged)
        self._data = MappingProxyType({key: entry.data for key, entry in merged.items()})

    def stale(self, *, keys: Iterable[str] | None = None, max_age: float | None = 300) -> tuple[str, ...]:
        """Return missing, incompatible or expired keys without I/O.

        With no keys supplied, inspect configured version keys and stored keys.
        Zero forces refresh; None accepts any age. Future timestamps are stale
        with a finite TTL.
        """
        if max_age is not None and (not math.isfinite(max_age) or max_age < 0):
            raise ValueError("max_age must be finite and non-negative, or None")
        now = time.time()
        result = []
        for key in self.select(keys):
            entry = self.entries.get(key)
            if entry is None or entry.version != self.versions.get(key, 1):
                result.append(key)
            elif max_age is not None and not (max_age > 0 and 0 <= now - entry.collected_at < max_age):
                result.append(key)
        return tuple(result)

    def bind(self, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace must be a nonempty cache identity")
        if self.namespace is not None and self.namespace != namespace:
            raise ValueError("Use a separate Cache for each namespace")
        self.namespace = namespace

    async def load(self, backend: CacheBackend, *, namespace: str, keys: Iterable[str] | None = None) -> None:
        """Merge a backend snapshot, omitting incompatible versions.

        None selects every persisted key in this namespace. Values are retained
        as returned by the backend, without copying or model validation. A load
        failure leaves in-memory entries unchanged.
        """
        selected = None if keys is None else self.select(keys)
        self.bind(namespace)
        loaded = await backend.get_many(namespace, selected)
        entries = {
            key: cast(CacheEntry[T], entry)
            for key, entry in loaded.items()
            if (selected is None or key in selected) and entry.version == self.versions.get(key, 1)
        }
        self.merge(entries)

    async def save(self, backend: CacheBackend, *, namespace: str, keys: Iterable[str] | None = None) -> None:
        """Persist selected entries atomically, leaving other stored keys intact.

        Do not mutate their values until this await completes. Replacing a
        branch in this cache does not change an already-started batch.
        """
        selected = self.select(keys)
        self.bind(namespace)
        await backend.set_many(namespace, {key: self.entries[key] for key in selected if key in self.entries})

    def invalidate(self, *, keys: Iterable[str] | None = None) -> None:
        """Forget selected in-memory entries without deleting persistent data."""
        removed = set(self.select(keys))
        remaining = {key: entry for key, entry in self.entries.items() if key not in removed}
        self.entries = MappingProxyType(remaining)
        self._data = MappingProxyType({key: entry.data for key, entry in remaining.items()})
