"""Remote callbacks released after the RPC has already returned."""

import asyncio
import logging
import os
from collections.abc import Callable

from rmote.protocol import Tool


def once(path: str, callback: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def ready() -> None:
        os.read(reader, 1)
        loop.remove_reader(reader)
        os.close(reader)
        callback()

    loop.add_reader(reader, ready)


class Idle(Tool):
    @staticmethod
    async def log(path: str, logger: str, message: str = "idle record") -> None:
        once(path, lambda: logging.getLogger(logger).warning("%s", message))

    @staticmethod
    async def exit(path: str) -> None:
        once(path, lambda: os._exit(0))
