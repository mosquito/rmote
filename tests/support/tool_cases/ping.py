"""The cheapest possible tool: it returns at once and carries no work."""

from rmote.protocol import Tool


class Ping(Tool):
    @staticmethod
    def ping() -> int:
        return 1
