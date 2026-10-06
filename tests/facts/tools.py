"""Tools used by real subprocess fact-collection tests."""

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from rmote.protocol import Tool


@dataclass
class CounterInfo:
    calls: int


@dataclass
class OverlapInfo:
    maximum: int
    pid: int


@dataclass
class Payload:
    values: dict[str, Any]


active = 0
maximum = 0
calls = 0
barrier: asyncio.Barrier | None = None


class Coordination(Tool):
    @staticmethod
    async def expect(concurrency: int) -> None:
        global barrier
        barrier = asyncio.Barrier(concurrency)


async def overlapping() -> OverlapInfo:
    global active, maximum
    active += 1
    maximum = max(maximum, active)
    try:
        if barrier is not None:
            await barrier.wait()
        return OverlapInfo(maximum, os.getpid())
    finally:
        active -= 1


class FirstFacts(Tool):
    result_type = OverlapInfo
    key = "first"
    version = 1

    @staticmethod
    async def collect() -> OverlapInfo:
        return await overlapping()


class SecondFacts(Tool):
    result_type = OverlapInfo
    key = "second"
    version = 1

    @staticmethod
    async def collect() -> OverlapInfo:
        return await overlapping()


class CounterFacts(Tool):
    result_type = CounterInfo
    key = "counter"
    version = 1

    @staticmethod
    def collect() -> CounterInfo:
        global calls
        calls += 1
        return CounterInfo(calls)


class BrokenFacts(Tool):
    result_type = CounterInfo
    key = "broken"
    version = 1

    @staticmethod
    def collect() -> CounterInfo:
        raise RuntimeError("collector failed")


class InvalidFacts(Tool):
    result_type = Payload
    key = "invalid"
    version = 1

    @staticmethod
    def collect() -> Payload:
        return Payload({"tuple": (1, 2)})
