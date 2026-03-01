from rmote.protocol import Tool


class BaseTool(Tool):
    @staticmethod
    def base_method() -> str:
        return "base"

    @staticmethod
    def shared(x: int) -> int:
        return x * 2


class DerivedTool(BaseTool):
    @staticmethod
    def derived_method() -> str:
        return "derived"
