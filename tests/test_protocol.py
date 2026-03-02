import asyncio

import pytest

from rmote.protocol import Protocol, Tool, tool_from_dict, tool_to_dict
from tests.tools_cases.same_name_a import SameNameTool as SameNameToolA
from tests.tools_cases.same_name_b import SameNameTool as SameNameToolB


class SimpleTool(Tool):
    @staticmethod
    def add(a: int, b: int) -> int:
        return a + b

    @staticmethod
    def echo(message: str) -> str:
        return f"Echo: {message}"


class AsyncTool(Tool):
    @staticmethod
    async def async_add(a: int, b: int) -> int:
        await asyncio.sleep(0.01)
        return a + b


def test_tool_serialization() -> None:
    """Test that Tool can be serialized to dict"""
    tool_dict = tool_to_dict(SimpleTool)

    assert tool_dict["name"] == "SimpleTool"
    assert "def add" in tool_dict["source"]
    assert "def echo" in tool_dict["source"]


def test_tool_deserialization() -> None:
    """Test that Tool can be reconstructed from dict"""
    tool_dict = tool_to_dict(SimpleTool)
    restored = tool_from_dict(tool_dict)

    assert restored.__name__ == "SimpleTool"
    instance = restored()
    assert instance.add(2, 3) == 5  # type: ignore[attr-defined]
    assert instance.echo("test") == "Echo: test"  # type: ignore[attr-defined]


def test_tool_roundtrip() -> None:
    """Test that Tool can be serialized and deserialized with all methods intact"""
    restored = tool_from_dict(tool_to_dict(SimpleTool))
    instance = restored()
    assert instance.add(10, 20) == 30  # type: ignore[attr-defined]
    assert instance.echo("roundtrip") == "Echo: roundtrip"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_protocol_subprocess(protocol: Protocol) -> None:
    """Test protocol with real subprocess"""
    # Call a simple tool
    result_int = await protocol(SimpleTool.add, 10, 32)
    assert result_int == 42

    result_str = await protocol(SimpleTool.echo, "Hello, rmote!")
    assert result_str == "Echo: Hello, rmote!"


@pytest.mark.asyncio
async def test_protocol_async_tool(protocol: Protocol) -> None:
    """Test protocol with async tool methods"""
    result = await protocol(AsyncTool.async_add, 100, 200)
    assert result == 300


@pytest.mark.asyncio
async def test_protocol_multiple_tools(protocol: Protocol) -> None:
    """Test protocol with multiple different tools"""
    # Use SimpleTool
    result1 = await protocol(SimpleTool.add, 5, 7)
    assert result1 == 12

    # Use AsyncTool
    result2 = await protocol(AsyncTool.async_add, 3, 4)
    assert result2 == 7

    # Use SimpleTool again
    result3 = await protocol(SimpleTool.echo, "test")
    assert result3 == "Echo: test"


@pytest.mark.asyncio
async def test_protocol_concurrent_calls(protocol):
    """Test concurrent RPC calls"""
    # Make multiple concurrent calls
    tasks = [protocol(SimpleTool.add, i, i + 1) for i in range(10)]

    results = await asyncio.gather(*tasks)

    # Verify all results
    for i, result in enumerate(results):
        assert result == i + (i + 1)


@pytest.mark.asyncio
async def test_same_name_tools(protocol: Protocol) -> None:
    """Test that tools with the same class name in different modules don't collide"""
    result_a = await protocol(SameNameToolA.value)
    assert result_a == "from_a"

    result_b = await protocol(SameNameToolB.value)
    assert result_b == "from_b"

    # Call both again to verify caching doesn't break things
    assert await protocol(SameNameToolA.value) == "from_a"
    assert await protocol(SameNameToolB.value) == "from_b"


@pytest.mark.asyncio
async def test_same_name_inline_tool(protocol: Protocol) -> None:
    """Test that an inline tool with the same class name as a file-level tool doesn't collide"""

    class SameNameTool(Tool):
        @staticmethod
        def value() -> str:
            return "from_inline"

    # Inline tool uses bare class name key, file-level tools use module-qualified key
    assert await protocol(SameNameTool.value) == "from_inline"
    assert await protocol(SameNameToolA.value) == "from_a"
    assert await protocol(SameNameToolB.value) == "from_b"

    # All three coexist without overwriting each other
    assert await protocol(SameNameTool.value) == "from_inline"


@pytest.mark.asyncio
async def test_dynamic_tools(protocol: Protocol) -> None:
    """Test dynamically defined inline tools work correctly"""

    class AlphaTool(Tool):
        @staticmethod
        def greet(name: str) -> str:
            return f"hello from alpha, {name}"

    class BetaTool(Tool):
        @staticmethod
        def greet(name: str) -> str:
            return f"hello from beta, {name}"

    assert await protocol(AlphaTool.greet, "world") == "hello from alpha, world"
    assert await protocol(BetaTool.greet, "world") == "hello from beta, world"

    # Mix with file-level tools in the same session
    assert await protocol(SameNameToolA.value) == "from_a"
    assert await protocol(AlphaTool.greet, "again") == "hello from alpha, again"
