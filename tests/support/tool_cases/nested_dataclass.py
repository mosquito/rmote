from dataclasses import dataclass
from enum import IntEnum

from rmote.protocol import Tool


class GeometryTool(Tool):
    class Unit(IntEnum):
        CM = 0
        MM = 1
        INCH = 2

    @dataclass
    class Point:
        x: float
        y: float = 0.0

    @staticmethod
    def distance(a: object, b: object) -> float:
        import math

        return math.hypot(b.x - a.x, b.y - a.y)  # type: ignore[attr-defined]

    @staticmethod
    def sum_coords(pt: object) -> float:
        return pt.x + pt.y  # type: ignore[attr-defined, no-any-return]
