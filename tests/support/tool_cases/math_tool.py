import math

from rmote.protocol import Tool


class MathTool(Tool):
    @staticmethod
    def floor(x: float) -> int:
        return math.floor(x)

    @staticmethod
    def sqrt(x: float) -> float:
        return math.sqrt(x)

    @staticmethod
    def hypot(a: float, b: float) -> float:
        return math.hypot(a, b)
