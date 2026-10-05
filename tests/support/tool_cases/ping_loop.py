"""The cheapest tool in three forms: thread, loop and coroutine."""

from rmote.protocol import Tool, inline


class PingForms(Tool):
    @staticmethod
    def thread() -> int:
        """A synchronous method, run in a worker thread by default."""
        return 1

    @staticmethod
    @inline
    def loop() -> int:
        """A synchronous method the author promised does not wait."""
        return 1

    @staticmethod
    async def coroutine() -> int:
        return 1
