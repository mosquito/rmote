# Tool result cache

`rmote.cache.Cache` stores results under caller-chosen string keys. It does not
know about facts, collectors or remote connections. Values retain their Python
types and identities. There is no automatic freezing or defensive deep copy.

```python
from rmote.cache import Cache

cache = Cache[object]()
result = {"running": True}
cache.update({"service-status": result})
assert cache["service-status"] is result
assert cache.data is cache.data
assert cache.stale(keys=["missing-result"]) == ("missing-result",)
```

The `data` property returns a read-only mapping of keys in O(1). Replacing a key
creates a new outer mapping; existing key snapshots continue to refer to their
previous values. Mutating a retained object affects every reader holding it.
The tool or caller owns that decision. For an independent deeply immutable
value, explicitly use {doc}`freeze <immutable>` before inserting it.

`Cache[T]` describes the value type, such as `Cache[str]` or `Cache[MyResult]`.
A heterogeneous cache can use `Cache[object]` with type narrowing, or `Cache[Any]`.
Facts-specific validation and `FactsData` belong to `rmote.tools.facts.validate`,
not to the cache.

`stale(keys=..., max_age=...)` reports missing, expired or version-mismatched
entries. Zero forces refresh; None accepts any age. With no keys supplied it
checks stored keys and keys declared in the optional `versions` mapping.
An empty unconfigured cache cannot guess which tool results the caller needs.
Versions default to 1; configure expected versions with
`Cache(versions={"service-status": 2})`.

Persistence is explicit:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio

from rmote.cache import Cache, JSONCacheDir

async def example(directory):
    backend = JSONCacheDir(Path(directory))
    cache = Cache[str]()
    cache.update({"command-output": "ready"})
    await cache.save(backend, namespace="user@server")
    restored = Cache[str]()
    await restored.load(backend, namespace="user@server")
    assert restored["command-output"] == "ready"

with TemporaryDirectory() as directory:
    asyncio.run(example(directory))
```

The first load/save binds the cache to one namespace. It can identify a host,
job, environment or another caller-defined scope. `load(keys=None)` reads all
stored entries in that namespace; an explicit key iterable selects a subset.
`invalidate()` affects memory only; use the backend's `delete()` for storage.
Older observations cannot replace newer ones. Backends merge batches atomically;
JSON uses one document replacement, and SQLite uses one transaction. JSON file
names use escaped namespaces with one-space indentation.

Keep values passed to `save`, `set` or `set_many` unchanged until completion.
Replacing a cache branch does not alter an already-started batch. Cancelling an
await may leave a worker finishing its atomic write. The in-memory cache accepts
arbitrary values; each backend defines its serialization limits. Failed
serialization does not partially commit a batch.

Both bundled backends use the {doc}`JSON hooks <serialization>` for dataclasses
and base64 bytes. Other values follow standard JSON rules. This is a storage
representation choice, not a restriction on the in-memory cache.

```{eval-rst}
.. autoclass:: rmote.cache.Cache
   :members:

.. automodule:: rmote.cache.backends
   :members: CacheBackend, CacheEntry, JSONCacheDir, SQLiteCache
```
