"""Two transferred classes that share module state and one import side effect."""

import logging

from rmote.protocol import Tool

logging.getLogger("rmote-concurrent-module").warning("concurrent module loaded")
_state = {"count": 0}


class FirstCounter(Tool):
    @staticmethod
    async def increment(token: int) -> tuple[int, int, int]:
        _state["count"] += 1
        return token, _state["count"], id(_state)


class SecondCounter(Tool):
    @staticmethod
    async def increment(token: int) -> tuple[int, int, int]:
        _state["count"] += 1
        return token, _state["count"], id(_state)
