"""Inspect receive configuration in the bootstrapped endpoint."""

import asyncio
import gc
import threading

from rmote.protocol import Protocol, Tool


class DecodeProbe(Tool):
    @staticmethod
    async def inspect() -> list[int]:
        return [obj.decode_threshold for obj in gc.get_objects() if isinstance(obj, Protocol)]

    @staticmethod
    async def thread(received_on: object, padding: bytes) -> bool:
        return received_on == threading.get_ident() and bool(asyncio.get_running_loop()) and bool(padding)


class DecodeThread:
    def __reduce__(self):
        return threading.get_ident, ()
