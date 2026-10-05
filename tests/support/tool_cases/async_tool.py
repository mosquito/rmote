import asyncio

from rmote.protocol import Tool


class AsyncTool(Tool):
    @staticmethod
    async def async_add(a: int, b: int) -> int:
        await asyncio.sleep(0.01)
        return a + b

    @staticmethod
    async def async_echo(message: str) -> str:
        await asyncio.sleep(0)
        return f"Async: {message}"
