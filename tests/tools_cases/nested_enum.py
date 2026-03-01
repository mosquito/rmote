from enum import IntEnum

from rmote.protocol import Tool


class ColorTool(Tool):
    class Color(IntEnum):
        RED = 1
        GREEN = 2
        BLUE = 3

    @staticmethod
    def name_of(value: int) -> str:
        return ColorTool.Color(value).name

    @staticmethod
    def is_primary(value: int) -> bool:
        return value in (ColorTool.Color.RED, ColorTool.Color.GREEN, ColorTool.Color.BLUE)
