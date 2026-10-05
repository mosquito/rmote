"""Remote tools for transport integration, with only standard library imports."""

from rmote.protocol import Tool


class TransportChecks(Tool):
    @staticmethod
    def echo(value: bytes) -> bytes:
        return value

    @staticmethod
    async def async_echo(value: bytes) -> bytes:
        import asyncio

        await asyncio.sleep(0)
        return value

    @staticmethod
    def interpreter() -> tuple[bool, bool, bool, bool]:
        import sys

        return (
            bool(sys.flags.isolated),
            bool(sys.flags.no_site),
            "rmote.sync" in sys.modules,
            "rmote._runtime" in sys.modules,
        )
