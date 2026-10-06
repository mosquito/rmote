"""A tool that emits a controlled burst of log records."""

import asyncio
import logging

from rmote.protocol import Tool


class LogSpam(Tool):
    pending: asyncio.Event | None = None

    @staticmethod
    def speak(count: int, label: str = "record") -> int:
        """Emit *count* records from a worker thread of the remote side."""
        logger = logging.getLogger("delivery-test")
        for index in range(count):
            logger.warning("%s %s", label, index)
        return count

    @staticmethod
    async def speak_on_loop(count: int, label: str = "record") -> int:
        """Emit *count* records on the loop thread of the remote side.

        The records then reach the handler in the step that sends the response,
        so a test needs no timing margin to see them travel with it.
        """
        logger = logging.getLogger("delivery-test")
        for index in range(count):
            logger.warning("%s %s", label, index)
        return count

    @staticmethod
    async def speak_until_released() -> int:
        LogSpam.pending = asyncio.Event()
        logging.getLogger("delivery-test").warning("record 0")
        await LogSpam.pending.wait()
        return 1

    @staticmethod
    async def release() -> None:
        if LogSpam.pending is not None:
            LogSpam.pending.set()
