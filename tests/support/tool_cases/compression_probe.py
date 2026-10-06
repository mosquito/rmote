"""Stdlib-only workloads shared by the transport compression experiment."""

from collections.abc import AsyncIterator

from rmote.protocol import Tool, inline


class CompressionProbe(Tool):
    @staticmethod
    @inline
    def fail() -> None:
        raise ValueError("failure " * 10000)

    @staticmethod
    @inline
    def keyword(*, compressed: bool) -> bool:
        return compressed

    @staticmethod
    @inline
    def echo(value: bytes) -> bytes:
        return value

    @staticmethod
    async def produce(count: int, value: bytes) -> AsyncIterator[bytes]:
        for _ in range(count):
            yield value
