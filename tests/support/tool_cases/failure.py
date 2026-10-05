from rmote.protocol import Tool


class FailTool(Tool):
    @staticmethod
    def always_fail(msg: str) -> None:
        raise RuntimeError(msg)

    @staticmethod
    async def async_fail(msg: str) -> None:
        raise ValueError(msg)
