from rmote.protocol import Tool


class SameNameTool(Tool):
    @staticmethod
    def value() -> str:
        return "from_b"
