"""Remote methods for TLS integration tests; only stdlib imports."""

import asyncio
from collections.abc import AsyncIterator

from rmote.protocol import Tool


class TLSMethods(Tool):
    @staticmethod
    async def echo(data: bytes) -> bytes:
        await asyncio.sleep(0)
        return data

    @staticmethod
    async def chunks(count: int, size: int) -> AsyncIterator[bytes]:
        for i in range(count):
            yield bytes([i % 256]) * size
