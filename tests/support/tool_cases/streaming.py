"""Controlled stream producers for subprocess protocol tests."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from rmote.protocol import Tool


class Streamer(Tool):
    gate: asyncio.Event
    pair: asyncio.Barrier | None = None

    @classmethod
    async def gated(cls) -> AsyncIterator[int]:
        cls.gate = asyncio.Event()
        yield 0
        await cls.gate.wait()
        yield 1

    @classmethod
    async def release(cls) -> None:
        cls.gate.set()

    @staticmethod
    async def counted(count: int) -> AsyncIterator[int]:
        for number in range(count):
            yield number

    @staticmethod
    async def chunks(count: int, size: int) -> AsyncIterator[bytes]:
        for number in range(count):
            yield bytes((number % 251,)) * size

    @classmethod
    async def paired(cls, count: int) -> AsyncIterator[int]:
        if cls.pair is None:
            cls.pair = asyncio.Barrier(2)
        for number in range(count):
            await cls.pair.wait()
            yield number

    @staticmethod
    async def failing() -> AsyncIterator[str]:
        yield "first"
        raise RuntimeError("generator failed")

    @staticmethod
    async def compressible(count: int, size: int) -> AsyncIterator[bytes]:
        for _ in range(count):
            yield b"a" * size

    @staticmethod
    async def blocks(path: str, size: int) -> AsyncIterator[bytes]:
        """Yield a file in bounded blocks, as a file tool would."""
        with open(path, "rb") as handle:
            while True:
                block = handle.read(size)
                if not block:
                    return
                yield block

    @staticmethod
    def add(left: int, right: int) -> int:
        return left + right


class Waiting(Tool):
    """A streaming method that waits for data it never receives."""

    closed: asyncio.Event

    @staticmethod
    async def forever(marker: str) -> AsyncIterator[int]:
        """Yield once, then wait. The cleanup writes *marker* as its proof."""

        Waiting.closed = asyncio.Event()
        try:
            yield 1
            await asyncio.Event().wait()
        finally:
            Path(marker).write_text("closed")
            Waiting.closed.set()

    @staticmethod
    async def wait_closed() -> None:
        await Waiting.closed.wait()

    @staticmethod
    def cleaned(marker: str) -> bool:
        """Report whether the cleanup of the generator has run."""

        return Path(marker).exists()
