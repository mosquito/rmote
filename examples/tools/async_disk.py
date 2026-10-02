import asyncio
import shutil

from rmote.protocol import Tool


class AsyncDisk(Tool):
    @staticmethod
    async def free_bytes(path: str) -> int:
        usage = await asyncio.to_thread(shutil.disk_usage, path)
        return usage.free
