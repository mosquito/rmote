"""Stdlib-only workloads shared by the transport compression experiment."""

import asyncio
from collections.abc import AsyncIterator

from rmote.protocol import Tool, inline


class CompressionProbe(Tool):
    @staticmethod
    @inline
    def fail() -> None:
        raise ValueError("failure " * 10000)

    @staticmethod
    @inline
    def keyword(*, compressed: bool) -> bool:
        return compressed

    @staticmethod
    @inline
    def echo(value: bytes) -> bytes:
        return value

    @staticmethod
    async def slow_reply() -> bytes:
        await asyncio.sleep(1)
        return b"done"

    @staticmethod
    async def produce(count: int, value: bytes) -> AsyncIterator[bytes]:
        for _ in range(count):
            yield value

    @staticmethod
    async def delayed() -> AsyncIterator[int]:
        yield 1
        await asyncio.sleep(0.05)
        yield 2
