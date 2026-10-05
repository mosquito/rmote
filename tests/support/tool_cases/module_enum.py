from enum import IntEnum

from rmote.protocol import Tool


class Direction(IntEnum):
    NORTH = 0
    EAST = 1
    SOUTH = 2
    WEST = 3


class DirectionTool(Tool):
    @staticmethod
    def opposite(direction: int) -> int:
        return Direction((direction + 2) % 4)

    @staticmethod
    def name_of(direction: int) -> str:
        return Direction(direction).name
