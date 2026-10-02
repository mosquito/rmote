"""Tools shared by the synchronous and asynchronous client examples."""

import asyncio

from rmote.protocol import Tool


class Echo(Tool):
    @staticmethod
    def echo(value: str) -> str:
        return value

    @staticmethod
    async def later(value: str, delay: float = 0.0) -> str:
        await asyncio.sleep(delay)
        return value
