"""Tests for Tool metaclass, serialization, and edge cases"""

import pytest

from rmote.protocol import Tool, tool_from_dict, tool_to_dict
from tests.tools_cases.config_tool import ConfigTool
from tests.tools_cases.custom_class import BBoxTool
from tests.tools_cases.inheritance import BaseTool, DerivedTool
from tests.tools_cases.json_tool import JsonTool
from tests.tools_cases.math_tool import MathTool
from tests.tools_cases.module_enum import Direction, DirectionTool
from tests.tools_cases.nested_dataclass import GeometryTool
from tests.tools_cases.nested_enum import ColorTool
from tests.tools_cases.simple import SimpleTool


class TestToolMetaclass:
    def test_tool_to_dict_shape(self) -> None:
        d = tool_to_dict(SimpleTool)
        assert d["name"] == "SimpleTool"
        assert "source" in d
        assert "def add" in d["source"]
        assert "def echo" in d["source"]

    def test_tool_cannot_have_init(self) -> None:
        with pytest.raises(TypeError, match="__init__ cannot be defined"):

            class BadTool(Tool):
                def __init__(self) -> None:
                    pass

    def test_tool_with_staticmethod(self) -> None:
        restored = tool_from_dict(tool_to_dict(SimpleTool))
        assert restored().add(2, 3) == 5  # type: ignore[attr-defined]
        assert restored().echo("hi") == "Echo: hi"  # type: ignore[attr-defined]

    def test_tool_with_classmethod(self) -> None:
        restored = tool_from_dict(tool_to_dict(SimpleTool))
        assert restored.class_name() == "SimpleTool"  # type: ignore[attr-defined]

    def test_tool_with_class_variables(self) -> None:
        restored = tool_from_dict(tool_to_dict(ConfigTool))
        assert restored.timeout == 30  # type: ignore[attr-defined]
        assert restored.max_retries == 5  # type: ignore[attr-defined]
        assert restored.get_timeout() == 30  # type: ignore[attr-defined]

    def test_tool_with_stdlib_import(self) -> None:
        restored = tool_from_dict(tool_to_dict(MathTool))
        assert restored().floor(3.7) == 3  # type: ignore[attr-defined]
        assert abs(restored().sqrt(9.0) - 3.0) < 1e-9  # type: ignore[attr-defined]

    def test_tool_import_alias(self) -> None:
        """Module-level alias (import json as _json) resolves correctly"""
        restored = tool_from_dict(tool_to_dict(JsonTool))
        assert restored().encode({"k": 1}) == '{"k": 1}'  # type: ignore[attr-defined]
        assert restored().decode('{"k": 1}') == {"k": 1}  # type: ignore[attr-defined]

    def test_tool_name_preserved(self) -> None:
        assert tool_from_dict(tool_to_dict(SimpleTool)).__name__ == "SimpleTool"

    def test_tool_with_nested_function(self) -> None:
        class NestedFnTool(Tool):
            @staticmethod
            def outer(x: int) -> int:
                def inner(y: int) -> int:
                    return y * 2

                return inner(x) + 1

        assert tool_from_dict(tool_to_dict(NestedFnTool))().outer(5) == 11  # type: ignore[attr-defined]

    def test_nested_intenum_roundtrip(self) -> None:
        restored = tool_from_dict(tool_to_dict(ColorTool))
        assert restored().name_of(1) == "RED"  # type: ignore[attr-defined]
        assert restored().name_of(3) == "BLUE"  # type: ignore[attr-defined]

    def test_nested_intenum_in_source(self) -> None:
        d = tool_to_dict(ColorTool)
        assert "IntEnum" in d["source"]

    def test_nested_dataclass_roundtrip(self) -> None:
        d = tool_to_dict(GeometryTool)
        assert "@dataclass" in d["source"]
        assert "dataclass" in d["source"]

        restored = tool_from_dict(d)
        pt = restored.Point(x=3, y=4)  # type: ignore[attr-defined]
        assert restored().sum_coords(pt) == 7.0  # type: ignore[attr-defined]

    def test_inheritance_roundtrip(self) -> None:
        d = tool_to_dict(DerivedTool)
        assert "BaseTool" in d["source"]

        restored = tool_from_dict(d, {"BaseTool": BaseTool})
        assert restored().derived_method() == "derived"  # type: ignore[attr-defined]

    def test_module_level_enum(self) -> None:
        """Non-nested enum defined at module level is transferred with its class"""
        d = tool_to_dict(DirectionTool)
        assert "Direction" in d["source"]

        restored = tool_from_dict(d)
        assert restored().name_of(Direction.NORTH) == "NORTH"  # type: ignore[attr-defined]
        assert restored().name_of(Direction.SOUTH) == "SOUTH"  # type: ignore[attr-defined]
        assert restored().opposite(Direction.NORTH) == Direction.SOUTH  # type: ignore[attr-defined]

    def test_module_level_custom_class(self) -> None:
        """Non-nested custom class defined at module level is transferred as a dependency"""
        d = tool_to_dict(BBoxTool)
        assert "BoundingBox" in d["source"]

        restored = tool_from_dict(d)
        box = restored().make(0, 0, 10, 5)  # type: ignore[attr-defined]
        assert restored().area(box) == 50.0  # type: ignore[attr-defined]
        assert restored().contains(box, 5, 3) is True  # type: ignore[attr-defined]
        assert restored().contains(box, 11, 3) is False  # type: ignore[attr-defined]

    def test_roundtrip_complex(self) -> None:
        restored = tool_from_dict(tool_to_dict(ConfigTool))
        assert restored.get_url() == "http://example.com"  # type: ignore[attr-defined]
