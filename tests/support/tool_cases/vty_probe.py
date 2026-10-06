"""Observe a Vty write reaching the subprocess pipe's drain."""

import asyncio

from rmote.protocol import Tool
from rmote.tools.vty import PipeSession, Vty


class VtyProbe(Tool):
    draining: asyncio.Event

    @staticmethod
    async def watch(key: int) -> None:
        session = Vty.sessions[key]
        assert isinstance(session, PipeSession)
        writer = session.process.stdin
        assert writer is not None
        original = writer.drain
        VtyProbe.draining = asyncio.Event()

        async def drain() -> None:
            VtyProbe.draining.set()
            await original()

        writer.drain = drain  # type: ignore[method-assign]

    @staticmethod
    async def wait() -> None:
        await VtyProbe.draining.wait()
