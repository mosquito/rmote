"""Remote collection and explicit local cache operations are independent."""

import asyncio
import os
import sys
import time
from dataclasses import asdict
from typing import Any, cast

import pytest

from rmote.cache import Cache, CacheEntry
from rmote.tools import facts
from rmote.tools.facts.schema import FactsData
from tests.facts.tools import (
    BrokenFacts,
    CounterFacts,
    CounterInfo,
    FirstFacts,
    InvalidFacts,
    OverlapInfo,
    Payload,
    SecondFacts,
)


class ProjectFactsData(FactsData, total=False):
    counter: CounterInfo
    first: OverlapInfo
    second: OverlapInfo
    invalid: Payload


@pytest.mark.asyncio
async def test_real_gather_explicit_save_offline_load(protocol, cache):
    result = await facts.fetch(protocol)
    assert set(result) == {
        "apt",
        "cpu",
        "memory",
        "pacman",
        "system",
        "python",
        "network",
        "networkd",
        "storage",
        "systemd",
        "systemd_timesync",
        "systemd_resolved",
    }
    assert result["system"].hostname
    assert result["python"].executable
    local = Cache[Any]()
    local.update(result)
    assert await cache.get("local", "python") is None
    await local.save(cache, namespace="local")
    loaded = Cache[Any](versions=dict.fromkeys(result, 1))
    await loaded.load(type(cache)(cache.path), namespace="local")
    assert loaded.data == result
    assert loaded.stale() == ()
    loaded.invalidate(keys=["system"])
    assert loaded.stale() == ("system",)
    assert await cache.get("local", "system") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_custom_collectors_transferred_and_run_on_remote(protocol, concurrency):
    result = cast(
        ProjectFactsData,
        await facts.fetch(protocol, collectors=[FirstFacts, SecondFacts], concurrency=concurrency),
    )
    assert result["second"].maximum == concurrency
    assert result["first"].pid == result["second"].pid != os.getpid()


@pytest.mark.asyncio
@pytest.mark.docker
async def test_gather_in_clean_container(docker_protocol, cache):
    result = await facts.fetch(docker_protocol)
    assert result["system"].os == "Linux"
    local = Cache[Any]()
    local.update(result)
    await local.save(cache, namespace="container")
    loaded = Cache[Any](versions=dict.fromkeys(result, 1))
    await loaded.load(cache, namespace="container")
    assert loaded.data == result


@pytest.mark.asyncio
async def test_cache_ttl_versions_hosts_and_explicit_refresh(protocol, cache):
    local = Cache[Any](versions={c.key: c.version for c in (CounterFacts,)})
    assert local.stale() == ("counter",)
    local.update(await facts.fetch(protocol, collectors=[CounterFacts]))
    assert local.stale() == ()
    assert local.stale(max_age=0) == ("counter",)
    assert cast(ProjectFactsData, local.data)["counter"].calls == 1
    await local.save(cache, namespace="a")
    with pytest.raises(ValueError, match="separate Cache"):
        await local.load(cache, namespace="b")
    assert await cache.get("b", "counter") is None
    local.update(await facts.fetch(protocol, collectors=[CounterFacts]))
    assert cast(ProjectFactsData, local.data)["counter"].calls == 2
    await cache.set("expired", "counter", CacheEntry(CounterInfo(40), time.time() - 1000))
    expired = Cache[Any](versions={c.key: c.version for c in (CounterFacts,)})
    await expired.load(cache, namespace="expired")
    assert expired.stale(max_age=None) == ()
    assert expired.stale(max_age=10) == ("counter",)
    await cache.set("version", "counter", CacheEntry(CounterInfo(50), time.time(), 2))
    incompatible = Cache[Any](versions={c.key: c.version for c in (CounterFacts,)})
    await incompatible.load(cache, namespace="version")
    assert incompatible.data == {}


@pytest.mark.asyncio
async def test_gather_failure_leaves_entire_cache_unchanged(protocol, cache):
    local = Cache[Any](
        versions={
            c.key: c.version
            for c in (
                CounterFacts,
                BrokenFacts,
            )
        }
    )
    local.update({"counter": CounterInfo(99)})
    await local.save(cache, namespace="host")
    before = local.data
    with pytest.raises(RuntimeError, match="collector failed"):
        local.update(await facts.fetch(protocol, collectors=[CounterFacts, BrokenFacts]))
    assert local.data == before
    assert (await cache.get("host", "counter")).data == CounterInfo(99)


def test_update_replacement_shares_original_values():
    local = Cache[Any]()
    values: dict[str, Any] = {"counter": CounterInfo(1), "invalid": Payload({"old": True})}
    local.update(values, collected_at=10)
    before = local.data
    assert local.data is before
    assert before["counter"] is values["counter"]
    local.update({"invalid": Payload({"new": True})}, collected_at=20)
    local.update({"counter": CounterInfo(0)}, collected_at=5)
    assert local["counter"].calls == 1
    assert before["invalid"].values == {"old": True}
    values["counter"].calls = 999
    assert local["counter"].calls == 999
    with pytest.raises(TypeError):
        cast(Any, local.data)["counter"] = CounterInfo(2)


@pytest.mark.asyncio
async def test_memory_backend_does_not_require_json(protocol):
    class MemoryCache:
        def __init__(self) -> None:
            self.entries: dict[tuple[str, str], CacheEntry[Any]] = {}

        async def get(self, host, key):
            return self.entries.get((host, key))

        async def set(self, host, key, entry):
            self.entries[host, key] = entry

        async def get_many(self, host, keys):
            return {
                key: entry
                for (owner, key), entry in self.entries.items()
                if owner == host and (keys is None or key in keys)
            }

        async def set_many(self, host, entries):
            self.entries.update({(host, key): entry for key, entry in entries.items()})

        async def delete(self, host, key):
            self.entries.pop((host, key), None)

    backend = MemoryCache()
    local = Cache[Any](versions={c.key: c.version for c in (InvalidFacts,)})
    local.update(await facts.fetch(protocol, collectors=[InvalidFacts]))
    await local.save(backend, namespace="host")
    restored = Cache[Any](versions={c.key: c.version for c in (InvalidFacts,)})
    await restored.load(backend, namespace="host")
    assert cast(ProjectFactsData, restored.data)["invalid"].values == {"tuple": (1, 2)}


@pytest.mark.asyncio
async def test_invalid_requests_and_empty_sections(protocol):
    refused: list[dict[str, Any]] = [
        dict(sections=["missing"]),
        dict(sections="system"),
        dict(concurrency=0),
        dict(collectors=[CounterFacts, CounterFacts]),
    ]
    for options in refused:
        with pytest.raises(ValueError):
            await facts.fetch(protocol, **options)
    assert await facts.fetch(protocol, sections=[]) == {}
    local = Cache[Any]()
    for age in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            local.stale(max_age=age)
        with pytest.raises(ValueError):
            local.update({}, collected_at=age)
    assert local.data == {}


@pytest.mark.asyncio
async def test_plain_dictionary_optional_sections_and_models(protocol):
    result = await facts.fetch(protocol, sections=["python", "python"])
    assert type(result) is dict and set(result) == {"python"}
    assert isinstance(asdict(result["python"]), dict)
    with pytest.raises(KeyError):
        result["network"]


@pytest.mark.asyncio
async def test_cancellation_stops_every_pending_call():
    """A cancelled fetch must not leave a call waiting for its answer."""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def remote(method):
        started.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    task = asyncio.create_task(facts.fetch(remote, collectors=[CounterFacts]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_one_failure_cancels_the_other_calls():
    """A failing branch must not leave its neighbours running."""
    running = 0
    cancelled = 0

    async def remote(method):
        nonlocal running, cancelled
        if method is BrokenFacts.collect:
            await asyncio.sleep(0)
            raise RuntimeError("collector failed")
        running += 1
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    with pytest.raises(RuntimeError, match="collector failed"):
        await facts.fetch(remote, collectors=[CounterFacts, BrokenFacts])
    assert running == cancelled == 1


@pytest.mark.asyncio
async def test_branch_of_a_wrong_type_is_refused():
    """Validation stays with the registry, whoever ran the collector."""

    async def remote(method):
        return CounterInfo(1)

    with pytest.raises(ValueError, match="must return"):
        await facts.fetch(remote, collectors=[InvalidFacts])


@pytest.mark.asyncio
async def test_gather_collects_the_local_host():
    """The same helper collects here when it is called directly."""
    result = await facts.gather(sections=["python", "system"])
    assert set(result) == {"python", "system"}
    assert result["python"].executable == sys.executable
