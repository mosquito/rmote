from rmote.protocol import Tool


class BoundingBox:
    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def area(self) -> float:
        return self.w * self.h

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


class BBoxTool(Tool):
    @staticmethod
    def area(box: object) -> float:
        return box.area()  # type: ignore[attr-defined, no-any-return]

    @staticmethod
    def contains(box: object, px: float, py: float) -> bool:
        return box.contains(px, py)  # type: ignore[attr-defined, no-any-return]

    @staticmethod
    def make(x: float, y: float, w: float, h: float) -> BoundingBox:
        return BoundingBox(x, y, w, h)
