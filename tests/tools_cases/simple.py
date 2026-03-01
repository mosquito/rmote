from rmote.protocol import Tool


class SimpleTool(Tool):
    @staticmethod
    def add(a: int, b: int) -> int:
        return a + b

    @staticmethod
    def echo(message: str) -> str:
        return f"Echo: {message}"

    @classmethod
    def class_name(cls) -> str:
        return cls.__name__
