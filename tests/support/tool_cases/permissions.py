"""Tools for endpoint permission checks in a bootstrapped interpreter."""

import gc

from rmote.protocol import Protocol, Tool


class Ping(Tool):
    @staticmethod
    def echo(value=42):
        return value


class Permissions(Tool):
    @staticmethod
    def inspect() -> list[tuple[bool, bool]]:
        return [(obj.accept_code, obj.accept_calls) for obj in gc.get_objects() if isinstance(obj, Protocol)]


class StrictProbe(Tool):
    @staticmethod
    def echo(value=42):
        return value

    @staticmethod
    def evil(marker: str, padding: bytes):
        class Payload:
            def __reduce__(self):
                return eval, (f"open({marker!r}, 'w').close()",)

        return [Payload(), padding]

    @staticmethod
    async def stream(marker: str, padding: bytes):
        yield StrictProbe.evil(marker, padding)

    @staticmethod
    def path():
        from pathlib import PurePosixPath

        return PurePosixPath("/approved/path")

    @staticmethod
    def decimal():
        from decimal import Decimal

        return Decimal("1.25")
