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
