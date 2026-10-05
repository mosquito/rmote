import asyncio
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import pytest

from rmote.cache import CacheEntry, JSONCacheDir, SQLiteCache
from rmote.cache.backends import JSONEntryCodec
from tests.cache.models import Payload


@pytest.mark.asyncio
async def test_cache_roundtrip_replacement_isolation(cache):
    data: dict[str, Any] = {
        "none": None,
        "bool": True,
        "int": 1,
        "float": 1.5,
        "str": "1",
        "list": [1, {"a.b": "ю"}],
        "empty": {},
    }
    await cache.set("../host", "system", CacheEntry(Payload(data), 1))
    await cache.set("../host", "python", CacheEntry(Payload({"version": "3"}), 1))
    assert await type(cache)(cache.path).get("../host", "system") == CacheEntry(Payload(data), 1)
    entry = await cache.get("../host", "system")
    entry.data.values["extra"] = True
    assert "extra" not in (await cache.get("../host", "system")).data.values
    await cache.set("../host", "system", CacheEntry(Payload({"replacement": True}), 2))
    assert (await cache.get("../host", "system")).data.values == {"replacement": True}
    assert (await cache.get("../host", "python")).data.values == {"version": "3"}
    assert await cache.get("other", "system") is None
    await cache.delete("../host", "system")
    await cache.delete("../host", "system")
    assert await cache.get("../host", "system") is None
    assert await cache.get("../host", "python") is not None


@pytest.mark.asyncio
async def test_older_write_cannot_replace_newer(cache):
    await cache.set("host", "system", CacheEntry(Payload({"new": True}), 20))
    await type(cache)(cache.path).set("host", "system", CacheEntry(Payload({"old": True}), 10))
    assert (await cache.get("host", "system")).data.values == {"new": True}


def process_write(backend, path, number, batch):
    cache = backend(path)

    async def write() -> None:
        if batch:
            entries = {
                f"collector-{number}-{index}": CacheEntry(Payload({"worker": number}), index) for index in range(4)
            }
            entries["ordered"] = CacheEntry(Payload({"worker": number}), number)
            await cache.set_many("shared", entries)
            return
        for index in range(4):
            await cache.set("shared", f"collector-{number}-{index}", CacheEntry(Payload({"worker": number}), index))
        await cache.set("shared", "ordered", CacheEntry(Payload({"worker": number}), number))

    asyncio.run(write())


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_concurrent_processes_preserve_all_branches(cache, batch):
    def run():
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(process_write, type(cache), cache.path, n, batch) for n in range(4)]
            for future in futures:
                future.result(timeout=30)

    await asyncio.to_thread(run)
    for n in range(4):
        for index in range(4):
            assert (await cache.get("shared", f"collector-{n}-{index}")).data.values == {"worker": n}
    assert (await cache.get("shared", "ordered")).data.values == {"worker": 3}


@pytest.mark.parametrize("data", [{"x": object()}, {"x": float("nan")}, {"x": float("inf")}])
@pytest.mark.asyncio
async def test_invalid_values_do_not_replace_cache(cache, data):
    await cache.set("host", "key", CacheEntry(Payload({"valid": True}), 1))
    with pytest.raises((TypeError, ValueError)):
        await cache.set("host", "key", CacheEntry(Payload(data), 2))
    assert (await cache.get("host", "key")).data.values == {"valid": True}


@pytest.mark.asyncio
async def test_cache_io_failure_is_not_a_miss(cache):
    cache.path.parent.joinpath("file").write_text("not a directory")
    broken = type(cache)(cache.path.parent / "file" / "cache")
    with pytest.raises(OSError):
        await broken.get("host", "key")


@pytest.mark.asyncio
async def test_corrupt_entry_propagates(cache):
    await cache.set("host", "key", CacheEntry(Payload({}), 1))
    if isinstance(cache, JSONCacheDir):
        next(cache.path.glob("*.json")).write_text('{"key": {"broken": true}}')
    else:
        cache.execute("UPDATE entries SET payload = ?", ('{"broken": true}',))
    with pytest.raises(ValueError):
        await cache.get("host", "key")


@pytest.mark.parametrize("payload", ["{}", "[]", "null", '{"data":{},"version":1,"collected_at":"x"}'])
def test_invalid_cache_entry(payload):
    with pytest.raises(ValueError):
        JSONEntryCodec.loads(payload)


def test_sqlite_requires_persistent_path():
    with pytest.raises(ValueError):
        SQLiteCache(":memory:")


@pytest.mark.asyncio
async def test_json_atomic_replace_failure_preserves_document(tmp_path, monkeypatch):
    cache = JSONCacheDir(tmp_path / "cache")
    await cache.set("host", "key", CacheEntry(Payload({"old": True}), 1))

    def fail(source, destination):
        raise OSError("disk error")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk error"):
        await cache.set("host", "key", CacheEntry(Payload({"new": True}), 2))
    entry = await cache.get("host", "key")
    assert entry is not None and entry.data.values == {"old": True}
    assert sorted(p.suffix for p in cache.path.iterdir()) == [".json", ".lock"]


@pytest.mark.asyncio
async def test_batch_ordering_selection_isolation_and_missing_keys(cache):
    original = {key: CacheEntry(Payload({"key": key}), 20) for key in ("old", "equal", "keep")}
    await cache.set_many("host", original)
    await cache.set_many(
        "host",
        {
            "old": CacheEntry(Payload({"ignored": True}), 10),
            "equal": CacheEntry(Payload({"replaced": True}), 20),
            "new": CacheEntry(Payload({"new": True}), 30),
        },
    )
    entries = await cache.get_many("host", iter(["old", "equal", "keep", "new", "absent", "old"]))
    assert entries == {
        "old": original["old"],
        "keep": original["keep"],
        "equal": CacheEntry(Payload({"replaced": True}), 20),
        "new": CacheEntry(Payload({"new": True}), 30),
    }
    entries["old"].data.values.clear()
    assert await cache.get("host", "old") == original["old"]
    assert await cache.get_many("other", entries) == {}


@pytest.mark.asyncio
async def test_invalid_batch_changes_nothing(cache):
    original = CacheEntry(Payload({"old": True}), 1)
    await cache.set("host", "first", original)
    with pytest.raises((TypeError, ValueError)):
        await cache.set_many(
            "host",
            {
                "first": CacheEntry(Payload({"new": True}), 2),
                "invalid": CacheEntry(Payload({"unsupported": object()}), 2),
            },
        )
    assert await cache.get_many("host", ["first", "invalid"]) == {"first": original}


@pytest.mark.asyncio
async def test_empty_batch_performs_no_io(cache):
    await cache.set_many("host", {})
    assert await cache.get_many("host", []) == {}
    assert not cache.path.exists()


@pytest.mark.asyncio
async def test_json_batch_reads_and_replaces_document_once(tmp_path, monkeypatch):
    from typing import Any
    from unittest.mock import Mock

    from rmote.cache import Cache

    backend = JSONCacheDir(tmp_path / "cache")
    local = Cache[Any]()
    local.update({f"branch-{index}": {"value": index} for index in range(9)})
    # Counting file reads shows that one batch touches the document once,
    # whichever way the batch then parses the text.
    read = Mock(wraps=backend.read_text)
    replace = Mock(wraps=os.replace)
    sync = Mock(wraps=os.fsync)
    monkeypatch.setattr(backend, "read_text", read)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", sync)
    await local.save(backend, namespace="host")
    assert read.call_count == replace.call_count == sync.call_count == 1
    read.reset_mock()
    restored = Cache[Any]()
    await restored.load(backend, namespace="host")
    assert read.call_count == 1
    assert restored.data == local.data


@pytest.mark.asyncio
async def test_batch_storage_failure_rolls_back(cache, monkeypatch):
    error: type[Exception]
    original = CacheEntry(Payload({"old": True}), 1)
    await cache.set("host", "first", original)
    if isinstance(cache, JSONCacheDir):

        def fail(source, destination):
            raise OSError("injected failure")

        monkeypatch.setattr(os, "replace", fail)
        error = OSError
    else:
        cache.execute(
            "CREATE TRIGGER fail_batch BEFORE INSERT ON entries WHEN NEW.key = 'second' "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END",
            (),
        )
        error = sqlite3.IntegrityError
    with pytest.raises(error, match="injected failure"):
        await cache.set_many(
            "host",
            {
                "first": CacheEntry(Payload({"new": True}), 2),
                "second": CacheEntry(Payload({"new": True}), 2),
            },
        )
    assert await cache.get_many("host", ["first", "second"]) == {"first": original}


@pytest.mark.asyncio
async def test_sqlite_large_key_batch(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    entries = {f"key-{index}": CacheEntry(Payload({"i": index}), 1) for index in range(1200)}
    await cache.set_many("host", entries)
    assert await cache.get_many("host", entries) == entries


@pytest.mark.asyncio
async def test_json_merge_leaves_untouched_branches_as_they_are(tmp_path, monkeypatch):
    """A merge orders branches by a number and never rebuilds their models."""
    from rmote.serialization import json_object_hook

    backend = JSONCacheDir(tmp_path / "cache")
    await backend.set_many("host", {f"branch-{index}": CacheEntry(Payload({"index": index}), 1) for index in range(5)})
    document = next(backend.path.glob("*.json")).read_text()

    built = []

    def counted(value):
        built.append(value)
        return json_object_hook(value)

    monkeypatch.setattr("rmote.cache.backends.json_object_hook", counted)
    await backend.set("host", "branch-9", CacheEntry(Payload({"index": 9}), 2))
    assert built == []
    # The branches that were already stored travel to the new document as they
    # were, byte for byte.
    assert document.rstrip("}\n").rstrip() in next(backend.path.glob("*.json")).read_text()
    assert (await backend.get("host", "branch-3")) == CacheEntry(Payload({"index": 3}), 1)


@pytest.mark.asyncio
async def test_json_read_builds_only_the_requested_branches(tmp_path, monkeypatch):
    from rmote.serialization import json_object_hook

    backend = JSONCacheDir(tmp_path / "cache")
    await backend.set_many("host", {f"branch-{index}": CacheEntry(Payload({"index": index}), 1) for index in range(8)})

    built = []

    def counted(value):
        if set(value) == {"_type", "_value"}:
            built.append(value)
        return json_object_hook(value)

    monkeypatch.setattr("rmote.cache.backends.json_object_hook", counted)
    entries = await backend.get_many("host", ("branch-2", "branch-5"))
    assert set(entries) == {"branch-2", "branch-5"}
    # Two Payload models, and nothing from the other six branches.
    assert len(built) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        '"collected_at": "soon", "version": 1, "format": 1',
        '"collected_at": -1, "version": 1, "format": 1',
        '"collected_at": 1, "version": 0, "format": 1',
        '"collected_at": 1, "version": 1, "format": 2',
        '"collected_at": 1, "version": 1',
    ],
)
async def test_json_merge_rejects_a_stored_branch_with_a_broken_header(tmp_path, header):
    """A merge checks the header of a stored branch, although it keeps its data."""
    backend = JSONCacheDir(tmp_path / "cache")
    await backend.set("host", "key", CacheEntry(Payload({}), 1))
    next(backend.path.glob("*.json")).write_text('{"key": {"data": {}, ' + header + "}}")
    with pytest.raises(ValueError):
        await backend.set("host", "key", CacheEntry(Payload({}), 2))
