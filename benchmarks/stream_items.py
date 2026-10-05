"""A streaming tool that yields fixed-size items without doing other work."""

import os
from collections.abc import AsyncIterator
from functools import lru_cache

from rmote.protocol import Tool


class Items(Tool):
    @staticmethod
    async def produce(count: int, size: int) -> AsyncIterator[bytes]:
        block = b"x" * size
        for _ in range(count):
            yield block

    @staticmethod
    def collect(count: int, size: int) -> list[bytes]:
        """Return the same items as one value, for comparison with a stream."""
        block = b"x" * size
        return [block] * count

    @staticmethod
    def echo(payload: bytes) -> bytes:
        """Return the payload, so it travels in both directions."""
        return payload

    @staticmethod
    def sink(payload: bytes) -> int:
        """Take a payload and report its size, so it travels one way."""
        return len(payload)

    @staticmethod
    def source(size: int, dense: bool) -> bytes:
        """Return *size* bytes that either compress well or not at all.

        The content is built once for a size, so a measurement of the wire
        does not include the cost of making it.
        """
        return Items.content(size, dense)

    @staticmethod
    @lru_cache(maxsize=8)
    def content(size: int, dense: bool) -> bytes:
        return os.urandom(size) if dense else (b"rmote " * (size // 6 + 1))[:size]
