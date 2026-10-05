"""Tools used by synchronous client tests, without client-side dependencies."""

import asyncio
import os
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
