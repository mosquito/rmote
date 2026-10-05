"""A tool that emits a controlled burst of log records."""

import asyncio
import logging
import time

from rmote.protocol import Tool


class LogSpam(Tool):
    @staticmethod
    def speak(count: int, delay: float = 0.0, label: str = "record") -> int:
        """Emit *count* records from a worker thread of the remote side."""
        logger = logging.getLogger("delivery-test")
        for index in range(count):
            logger.warning("%s %s", label, index)
            if delay:
                time.sleep(delay)
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
    async def speak_later(delay: float = 0.02, message: str = "from a timer") -> float:
        """Emit one record after this call returns, from a timer of the loop."""
        logger = logging.getLogger("delivery-test")
        asyncio.get_running_loop().call_later(delay, logger.warning, "%s", message)
        return delay
