"""Collect independent host facts with ordinary Tool classes.

``DEFAULT_COLLECTORS`` holds the active collectors. Two helpers join their
branches, and both validate every branch against its declared result_type:

``gather`` collects on the side that runs it. Through RPC it collects on the
target in one call: ``await remote(facts.gather, sections=["system"])``. Called
directly it collects the local host.

``fetch`` collects through a peer, with one RPC for each collector:
``await facts.fetch(remote, sections=["system"])``. The branches arrive as the
target produces them, and the caller decides how many run together.

Persistence and cache freshness belong to the controller's
:class:`rmote.cache.Cache`.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from functools import lru_cache, partial
from typing import Any, cast

from rmote.protocol import runs_on_loop

from .schema import DEFAULT_COLLECTORS, Collector, CollectorRegistry, FactsData

__tool_package__ = "rmote.tools.facts"

__all__ = ["DEFAULT_COLLECTORS", "Collector", "FactsData", "fetch", "gather", "validate"]

Remote = Callable[..., Awaitable[Any]]


@lru_cache(maxsize=128)
def drops_raw(collect: Callable[..., Any]) -> bool:
    """True when a collector lets the caller ask it to drop its raw texts.

    A collector without the keyword is collected as it is, with its texts.
    """
    try:
        return "raw" in inspect.signature(collect).parameters
    except (TypeError, ValueError):
        return False


def validate(
    values: Mapping[str, object],
    *,
    collectors: Iterable[Collector[Any]] = DEFAULT_COLLECTORS,
) -> FactsData:
    """Validate cached or externally supplied branches and retain their models.

    The returned dictionary shares the values. This optional facts-specific
    check is separate from the general-purpose cache.
    """
    registry = CollectorRegistry(collectors)
    result = dict(values)
    for key, value in result.items():
        registry.validate(key, value)
    return cast(FactsData, result)


async def join(
    registry: CollectorRegistry,
    keys: Iterable[str],
    call: Callable[[str], Awaitable[object]],
    concurrency: int,
) -> FactsData:
    """Run *call* for every key with a limit, and validate each branch.

    Implementation helper shared by fetch and gather, outside the public API.

    On failure or cancellation the unfinished calls are cancelled and awaited.
    Running synchronous code cannot be forcibly stopped. No partial result is
    returned.

    Raises:
        ValueError: *concurrency* is not a positive integer, or a branch does
            not match the declared result_type.
    """
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    selected = tuple(keys)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(key: str) -> object:
        async with semaphore:
            data = await call(key)
        registry.validate(key, data)
        return data

    tasks = [asyncio.create_task(one(key)) for key in selected]
    try:
        values = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return cast(FactsData, dict(zip(selected, values, strict=True)))


async def gather(
    *,
    sections: Iterable[str] | None = None,
    collectors: Iterable[Collector[Any]] = DEFAULT_COLLECTORS,
    concurrency: int = 8,
    raw: bool = True,
) -> FactsData:
    """Collect sections on this side, returning only after all have succeeded.

    Args:
        sections: Keys to collect, or None for every registered collector.
        collectors: Collector Tool classes with key, version and result_type.
            Tool classes passed as RPC arguments carry their source definitions.
        concurrency: Maximum simultaneous collectors in this call.
        raw: False asks every collector that can to drop the source texts it
            read. The parsed fields stay; the texts are most of a snapshot.

    Synchronous collectors run in worker threads.
    """
    registry = CollectorRegistry(collectors)
    texts: dict[str, Any] = {} if raw else {"raw": False}

    async def call(key: str) -> object:
        collector = registry.collectors[key]
        keywords = texts if drops_raw(collector.collect) else {}
        if inspect.iscoroutinefunction(collector.collect):
            return await collector.collect(**keywords)
        if runs_on_loop(collector.collect):
            # A collector that reads nothing but the interpreter itself does
            # not need a worker thread, which costs more than the call.
            return collector.collect(**keywords)
        data = await asyncio.to_thread(partial(collector.collect, **keywords))
        if inspect.isawaitable(data):
            data = await data
        return data

    return await join(registry, registry.select(sections), call, concurrency)


async def fetch(
    remote: Remote,
    *,
    sections: Iterable[str] | None = None,
    collectors: Iterable[Collector[Any]] = DEFAULT_COLLECTORS,
    concurrency: int = 8,
    raw: bool = True,
) -> FactsData:
    """Collect sections through *remote*, with one call for each collector.

    Each collector travels as an ordinary Tool and runs in its own RPC, so a
    branch arrives as soon as the target produces it, and a long branch does
    not hold the others.

    Args:
        remote: Callable that performs one RPC, such as a Protocol instance.
        sections: Keys to collect, or None for every registered collector.
        collectors: Collector Tool classes with key, version and result_type.
        concurrency: Maximum simultaneous calls.
        raw: False asks every collector that can to drop the source texts it
            read, so they never reach the wire.

    Cancelling this call stops local waiting. It does not stop work already
    running on the target. Call the collectors separately when a failing branch
    must not affect the others.
    """
    registry = CollectorRegistry(collectors)
    texts: dict[str, Any] = {} if raw else {"raw": False}

    async def call(key: str) -> object:
        collector = registry.collectors[key]
        return await remote(collector.collect, **(texts if drops_raw(collector.collect) else {}))

    return await join(registry, registry.select(sections), call, concurrency)
