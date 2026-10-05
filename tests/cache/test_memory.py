import asyncio
import subprocess
import sys
from typing import Any, cast

import pytest

from rmote.cache import Cache, CacheEntry
from rmote.immutable import freeze
from rmote.protocol import Tool


def test_values_are_retained_without_copying_or_freezing():
    class Result:
        def __deepcopy__(self, memo):
            raise AssertionError("must not copy")

    value = Result()
    result = subprocess.CompletedProcess(["command"], 0, b"output")
    cache = Cache[Any]()
    cache.update({"object": value, "process": result, "list": []})
    snapshot = cache.data
    assert cache.data is snapshot
    assert cache["object"] is value
    assert cache["process"] is result
    cache["list"].append(1)
    assert snapshot["list"] == [1]
    cache.update({"list": [2]})
    assert snapshot["list"] == [1]
    assert cache["list"] == [2]
    with pytest.raises(TypeError):
        cast(Any, cache.data)["object"] = None


def test_explicit_freeze_is_reused():
    cache = Cache[Any]()
    snapshot = freeze({"values": [1]})
    cache.update({"result": snapshot})
    assert cache["result"] is snapshot


def test_requested_missing_keys_versions_and_atomic_update():
    cache = Cache[int](versions={"configured": 2})
    assert cache.stale(keys=["new-tool"]) == ("new-tool",)
    cache.update({"configured": 1, "new-tool": 2}, collected_at=20)
    assert cache.entries["configured"].version == 2
    assert cache.entries["new-tool"].version == 1
    before = cache.data
    with pytest.raises(ValueError, match="cache key"):
        cache.update({"configured": 3, "": 4})
    assert cache.data is before
    cache.update({"configured": 4}, collected_at=10)
    assert cache["configured"] == 1
    cache.invalidate(keys=["new-tool"])
    assert cache.stale(keys=["new-tool"]) == ("new-tool",)


@pytest.mark.asyncio
async def test_save_batch_retains_original_values_during_replacement():
    started, finish = asyncio.Event(), asyncio.Event()
    original = object()
    captured = {}

    class Backend:
        async def set_many(self, namespace, entries):
            captured.update(entries)
            started.set()
            await finish.wait()

    cache = Cache[object]()
    cache.update({"key": original})
    task = asyncio.create_task(cache.save(cast(Any, Backend()), namespace="scope"))
    await started.wait()
    cache.update({"key": object()})
    finish.set()
    await task
    assert captured["key"].data is original


@pytest.mark.asyncio
async def test_generic_tool_result_roundtrip(protocol, cache):
    class OrdinaryTool(Tool):
        @staticmethod
        def run():
            return {"code": 0, "output": ["hello"], "nothing": None}

    result = await protocol(OrdinaryTool.run)
    memory = Cache[Any]()
    memory.update({"ordinary-command": result})
    assert memory["ordinary-command"] is result
    await memory.save(cache, namespace="job:42")
    restored = Cache[Any]()
    await restored.load(cache, namespace="job:42")
    assert restored["ordinary-command"] == result
    assert await cache.get_many("job:42", None) == dict(memory.entries)


@pytest.mark.asyncio
async def test_load_preserves_memory_on_backend_failure(cache):
    memory = Cache[int]()
    memory.update({"key": 1}, collected_at=20)
    await cache.set_many("scope", {"key": CacheEntry(2, 10), "other": CacheEntry(3, 30)})
    await memory.load(cache, namespace="scope")
    assert dict(memory) == {"key": 1, "other": 3}
    before = memory.data

    class Broken:
        async def get_many(self, namespace, keys):
            raise OSError("read failed")

    with pytest.raises(OSError):
        await memory.load(cast(Any, Broken()), namespace="scope")
    assert memory.data is before


def test_importing_cache_does_not_load_fact_collectors():
    script = "import sys, rmote.cache; assert not any(n.startswith('rmote.tools.facts') for n in sys.modules)"
    subprocess.run([sys.executable, "-c", script], check=True)
