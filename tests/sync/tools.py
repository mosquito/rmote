"""Tools used by synchronous client tests, without client-side dependencies."""

import asyncio
import hashlib
import os
import random
from collections.abc import AsyncIterator
from pathlib import Path

from rmote.protocol import Tool


class Environment(Tool):
    @staticmethod
    def inspect():
        return os.getcwd(), os.environ.get("RMOTE_SYNC_TEST")


class Methods(Tool):
    @staticmethod
    def echo(value: str) -> str:
        return value

    @staticmethod
    async def async_echo(value: str) -> str:
        await asyncio.sleep(0)
        return value

    @classmethod
    def class_echo(cls, value: str) -> str:
        return value

    @classmethod
    async def async_class_echo(cls, value: str) -> str:
        await asyncio.sleep(0)
        return value

    @staticmethod
    def keywords(*, timeout: float, tool: str) -> tuple[float, str]:
        return timeout, tool

    @staticmethod
    async def stream(marker: str) -> AsyncIterator[int]:
        Path(marker).write_text("started")
        yield 1

    @staticmethod
    def fail() -> None:
        raise FileNotFoundError("remote file")

    @staticmethod
    async def pause(delay: float, marker: str) -> str:
        Path(marker).write_text("started")
        await asyncio.sleep(delay)
        Path(marker).write_text("finished")
        return "finished"

    @staticmethod
    def exit() -> None:
        os._exit(0)


class Load(Tool):
    """Methods for load tests. Every answer identifies the call that made it."""

    @staticmethod
    def ping(token: str) -> str:
        return token

    @staticmethod
    def total(start: int, count: int) -> int:
        """Add *count* consecutive integers, from *start*.

        The caller checks the answer against a closed form, so a result that
        belongs to another call does not pass.
        """
        return sum(range(start, start + count))

    @staticmethod
    async def power(base: int, exponent: int) -> int:
        """Raise *base* to *exponent*. Large results need several frames."""
        await asyncio.sleep(0)
        # A negative exponent gives a float, so the type checker sees Any here.
        result: int = base**exponent
        return result

    @staticmethod
    def fail(token: str) -> None:
        raise ValueError(f"failed:{token}")

    @staticmethod
    async def pause(delay: float, token: str) -> str:
        await asyncio.sleep(delay)
        return token

    @staticmethod
    def roundtrip(token: int, payload: bytes, size: int) -> tuple[int, str, bytes]:
        """Digest *payload* and answer with *size* bytes that do not compress.

        The answer comes from a generator seeded with *token*, so the caller
        reproduces the expected bytes without receiving them twice.
        """
        return token, hashlib.sha256(payload).hexdigest(), random.Random(token).randbytes(size)


class Second(Tool):
    """A second class in this module. Each class needs its own transfer."""

    @staticmethod
    def ping(token: str) -> str:
        return token
