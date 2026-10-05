"""Persistent caches with atomic batches and per-value write ordering."""

import asyncio
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar
from urllib.parse import quote

from rmote.serialization import json_default, json_object_hook

T = TypeVar("T")


@dataclass(frozen=True)
class CacheEntry(Generic[T]):
    """One complete value result.

    Args:
        data: Any tool result supported by the selected backend.
        collected_at: Controller observation timestamp, in Unix seconds. Also orders writes:
            an older collection cannot overwrite a newer cached result.
        version: Positive value schema version; changed versions force refresh.

    The bundled persistent backends reconstruct values on load. Treat entries passed to ``set``
    or ``set_many`` as immutable until the call completes. Ordering assumes the controller
    processes share a reasonably stable wall clock.
    """

    data: T
    collected_at: float
    version: int = 1


class JSONEntryCodec:
    """JSON representation shared by the bundled persistent backends."""

    @staticmethod
    def validate(entry: CacheEntry[Any]) -> None:
        if (
            type(entry.collected_at) not in (float, int)
            or not math.isfinite(entry.collected_at)
            or entry.collected_at < 0
        ):
            raise ValueError("collected_at must be finite and non-negative")
        if type(entry.version) is not int or entry.version < 1:
            raise ValueError("version must be a positive integer")

    @staticmethod
    def encode(entry: CacheEntry[Any]) -> dict[str, Any]:
        """Wrap an entry without copying or pre-serializing its data."""
        JSONEntryCodec.validate(entry)
        return {
            "data": entry.data,
            "collected_at": entry.collected_at,
            "version": entry.version,
            "format": 1,
        }

    @staticmethod
    def decode(value: Any) -> CacheEntry[Any]:
        """Restore an entry directly from a parsed document."""
        try:
            data = value["data"]
            if type(value["format"]) is not int or value["format"] != 1:
                raise ValueError("Unknown cache format")
            entry = CacheEntry(data, value["collected_at"], value["version"])
            JSONEntryCodec.validate(entry)
            return entry
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid cache entry") from exc

    @staticmethod
    def stored_at(value: Any) -> float:
        """Return the collection time of a stored branch, leaving its data alone.

        A merge only orders branches against each other, so it checks the
        format and the numbers and never rebuilds the models of the data.
        """
        try:
            if type(value["format"]) is not int or value["format"] != 1:
                raise ValueError("Unknown cache format")
            JSONEntryCodec.validate(CacheEntry(None, value["collected_at"], value["version"]))
            return float(value["collected_at"])
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid cache entry") from exc

    @staticmethod
    def revive(value: Any) -> Any:
        """Rebuild the models of one branch that was read without them.

        The containers are walked bottom-up, exactly as the parser applies the
        hook, because encoding the branch back to text and parsing it again
        costs more than the walk.
        """
        if type(value) is dict:
            return json_object_hook({name: JSONEntryCodec.revive(item) for name, item in value.items()})
        if type(value) is list:
            return [JSONEntryCodec.revive(item) for item in value]
        return value

    @staticmethod
    def dumps(entry: CacheEntry[Any]) -> str:
        return json.dumps(JSONEntryCodec.encode(entry), default=json_default, allow_nan=False)

    @staticmethod
    def loads(payload: str) -> CacheEntry[Any]:
        return JSONEntryCodec.decode(json.loads(payload, object_hook=json_object_hook))


class CacheBackend(Protocol):
    """Async storage contract shared by persistent cache backends.

    ``set_many`` atomically replaces selected branches, leaving other keys intact.
    It ignores writes older than the stored collection start time. Operations
    must be safe across instances and processes. Missing keys return ``None``
    from ``get`` and are omitted by ``get_many``;
    I/O failures and corrupt data propagate rather than becoming cache misses.
    Blocking storage work must not run on the caller's event loop.
    """

    async def get_many(self, namespace: str, keys: Iterable[str] | None) -> dict[str, CacheEntry[Any]]:
        """Read selected branches from one snapshot, omitting missing keys."""
        ...

    async def set_many(self, namespace: str, entries: Mapping[str, CacheEntry[Any]]) -> None:
        """Atomically merge a batch, ignoring older writes independently per key."""
        ...

    async def get(self, namespace: str, key: str) -> CacheEntry[Any] | None:
        """Read one branch, including metadata, without checking its age."""
        ...

    async def set(self, namespace: str, key: str, entry: CacheEntry[Any]) -> None:
        """Replace a branch unless a newer collection is already stored."""
        ...

    async def delete(self, namespace: str, key: str) -> None:
        """Remove a branch if present. Does not cancel an in-flight collection."""
        ...


class JSONCacheDir:
    """Store one JSON document per namespace in a local directory (POSIX).

    Documents use namespace names, for example ``admin@server.json``, and JSON
    indentation of one space. Path separators and percent signs are encoded
    so namespace identities cannot escape the directory or alias escaped names.
    A stable per-namespace flock protects read/modify/write
    across processes; writes use fsync and atomic replacement. Lock files are
    retained to keep every process locking the same inode. Use a local
    filesystem with working POSIX locks and atomic rename.

    The directory is created lazily. New directories and files are private to
    the current user. No persistent handles need closing. Disk operations run
    in worker threads; cancellation may leave an already-started write running.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @contextmanager
    def locked(self, namespace: str) -> Iterator[Path]:
        """Lock a namespace document for a short filesystem operation."""
        import fcntl

        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = quote(namespace, safe="@[]:")
        fd = os.open(self.path / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self.path / f"{name}.json"
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def read_text(path: Path) -> str:
        """Return the text of a document. An absent file reads as empty."""
        try:
            return path.read_text()
        except FileNotFoundError:
            return ""

    @staticmethod
    def parse_document(text: str, *, decode: bool = True) -> dict[str, Any]:
        """Parse a document, with or without rebuilding the stored models.

        Without *decode* the branches stay plain JSON containers. A merge and
        an age comparison need only numbers and opaque branches, and rebuilding
        the models of a whole document, then encoding them back, costs more
        than everything else a write does.
        """
        if not text:
            return {}
        value = json.loads(text, object_hook=json_object_hook) if decode else json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Invalid cache document")
        return value

    @staticmethod
    def read_document(path: Path, *, decode: bool = True) -> dict[str, Any]:
        """Read a document, treating only an absent file as an empty cache."""
        return JSONCacheDir.parse_document(JSONCacheDir.read_text(path), decode=decode)

    def read_entry(self, namespace: str, key: str) -> CacheEntry[Any] | None:
        return self.read_entries(namespace, (key,)).get(key)

    def read_entries(self, namespace: str, keys: Iterable[str] | None) -> dict[str, CacheEntry[Any]]:
        keys = None if keys is None else tuple(dict.fromkeys(keys))
        if keys == ():
            return {}
        with self.locked(namespace) as path:
            text = self.read_text(path)
        if keys is None:
            return {key: JSONEntryCodec.decode(value) for key, value in self.parse_document(text).items()}
        # A read of a few branches parses the document without models and
        # rebuilds only what it returns.
        document = self.parse_document(text, decode=False)
        selected = [key for key in keys if key in document]
        if len(selected) * 2 >= len(document):
            # Rebuilding most of a document branch by branch costs more than
            # parsing the whole text once with the models.
            decoded = self.parse_document(text)
            return {key: JSONEntryCodec.decode(decoded[key]) for key in selected}
        return {key: JSONEntryCodec.decode(JSONEntryCodec.revive(document[key])) for key in selected}

    def write_entry(self, namespace: str, key: str, payload: str | None) -> None:
        self.write_entries(namespace, {key: JSONEntryCodec.loads(payload) if payload is not None else None})

    def write_entries(self, namespace: str, entries: Mapping[str, CacheEntry[Any] | None]) -> None:
        # Validate/encode the whole batch before taking the lock or changing disk.
        encoded = {key: JSONEntryCodec.encode(entry) if entry is not None else None for key, entry in entries.items()}
        if not encoded:
            return
        with self.locked(namespace) as path:
            document = self.parse_document(self.read_text(path), decode=False)
            changed = False
            for key, value in encoded.items():
                if value is None:
                    if key in document:
                        del document[key]
                        changed = True
                else:
                    if key in document and JSONEntryCodec.stored_at(document[key]) > value["collected_at"]:
                        continue
                    document[key] = value
                    changed = True
            if not changed:
                return
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=self.path, delete=False, encoding="utf-8") as stream:
                    temporary = Path(stream.name)
                    stream.write(json.dumps(document, default=json_default, allow_nan=False, indent=1))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    async def get(self, namespace: str, key: str) -> CacheEntry[Any] | None:
        """Read a value entry or return None when absent."""
        return (await self.get_many(namespace, (key,))).get(key)

    async def set(self, namespace: str, key: str, entry: CacheEntry[Any]) -> None:
        """Atomically replace one value branch if it is not older."""
        await self.set_many(namespace, {key: entry})

    async def get_many(self, namespace: str, keys: Iterable[str] | None) -> dict[str, CacheEntry[Any]]:
        """Read all selected branches with one lock and one document parse."""
        return await asyncio.to_thread(self.read_entries, namespace, None if keys is None else tuple(keys))

    async def set_many(self, namespace: str, entries: Mapping[str, CacheEntry[Any]]) -> None:
        """Encode and atomically merge a batch with one document replacement."""
        await asyncio.to_thread(self.write_entries, namespace, dict(entries))

    async def delete(self, namespace: str, key: str) -> None:
        """Delete one branch while preserving every other value."""
        await asyncio.to_thread(self.write_entries, namespace, {key: None})


class SQLiteCache:
    """Store JSON value entries in a local SQLite database.

    The primary key is ``(namespace, key)``. Conditional UPSERT prevents older
    collections from overwriting newer results. Each operation opens a short
    connection in a worker thread; a batch shares one transaction. Encoding and
    decoding also run in that thread; transactions never span remote collection.
    Concurrent writers wait up to ``timeout`` seconds before SQLite raises.
    The parent directory must exist. ``:memory:`` is intentionally unsupported;
    this backend persists across connections and runs. No close is needed.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 5.0) -> None:
        if str(path) == ":memory:":
            raise ValueError("SQLiteCache requires a filesystem path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.path = Path(path)
        self.timeout = timeout

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # Restrict permissions when creating a new database, without changing
        # permissions on an existing one or truncating it.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "namespace TEXT NOT NULL, key TEXT NOT NULL, collected_at REAL NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY (namespace, key))"
            )
            yield connection

    def execute(self, query: str, parameters: tuple[Any, ...]) -> Any:
        with self.connection() as connection:
            return connection.execute(query, parameters).fetchone()

    def read_entries(self, namespace: str, keys: Iterable[str] | None) -> dict[str, CacheEntry[Any]]:
        keys = None if keys is None else tuple(dict.fromkeys(keys))
        if keys == ():
            return {}
        rows = []
        with self.connection() as connection:
            # Keep a consistent snapshot even when the key list needs several
            # queries to stay below SQLite's bound-parameter limit.
            connection.execute("BEGIN")
            if keys is None:
                rows = connection.execute(
                    "SELECT key, payload FROM entries WHERE namespace = ?", (namespace,)
                ).fetchall()
            for offset in range(0, len(keys or ()), 500):
                assert keys is not None
                batch = keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        f"SELECT key, payload FROM entries WHERE namespace = ? AND key IN ({placeholders})",
                        (namespace, *batch),
                    ).fetchall()
                )
        return {key: JSONEntryCodec.loads(payload) for key, payload in rows}

    def write_entries(self, namespace: str, entries: Mapping[str, CacheEntry[Any]]) -> None:
        rows = [(namespace, key, entry.collected_at, JSONEntryCodec.dumps(entry)) for key, entry in entries.items()]
        if not rows:
            return
        with self.connection() as connection:
            connection.executemany(
                "INSERT INTO entries (namespace, key, collected_at, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (namespace, key) DO UPDATE SET collected_at = excluded.collected_at, payload = excluded.payload "
                "WHERE excluded.collected_at >= entries.collected_at",
                rows,
            )

    async def get(self, namespace: str, key: str) -> CacheEntry[Any] | None:
        """Read a value entry or return None when absent."""
        return (await self.get_many(namespace, (key,))).get(key)

    async def set(self, namespace: str, key: str, entry: CacheEntry[Any]) -> None:
        """Atomically replace one branch unless a newer collection exists."""
        await self.set_many(namespace, {key: entry})

    async def get_many(self, namespace: str, keys: Iterable[str] | None) -> dict[str, CacheEntry[Any]]:
        """Read selected branches from one transaction."""
        return await asyncio.to_thread(self.read_entries, namespace, None if keys is None else tuple(keys))

    async def set_many(self, namespace: str, entries: Mapping[str, CacheEntry[Any]]) -> None:
        """Validate the batch, then merge it in one transaction."""
        await asyncio.to_thread(self.write_entries, namespace, dict(entries))

    async def delete(self, namespace: str, key: str) -> None:
        """Delete one branch, doing nothing when it is absent."""
        await asyncio.to_thread(self.execute, "DELETE FROM entries WHERE namespace = ? AND key = ?", (namespace, key))
