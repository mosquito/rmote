"""Tests for protocol error handling and edge cases"""

import asyncio
import math
import sys
from typing import Any

import pytest

from rmote.protocol import BaseProtocol, Protocol, Tool


class ValidTool(Tool):
    @staticmethod
    def method() -> str:
        return "valid"


class LargePayloadTool(Tool):
    @staticmethod
    def echo(data: str) -> str:
        return data


class NestedDataTool(Tool):
    @staticmethod
    def echo(data: dict[str, Any]) -> dict[str, Any]:
        return data


class SpecialCharTool(Tool):
    @staticmethod
    def echo(text: str) -> str:
        return text


class AlwaysFailTool(Tool):
    @staticmethod
    def fail(msg: str) -> None:
        raise RuntimeError(msg)


class EmptyCollectionTool(Tool):
    @staticmethod
    def echo_list(data: list[Any]) -> list[Any]:
        return data

    @staticmethod
    def echo_dict(data: dict[str, Any]) -> dict[str, Any]:
        return data

    @staticmethod
    def echo_set(data: set[Any]) -> set[Any]:
        return data


class NumericTool(Tool):
    @staticmethod
    def echo_int(val: int) -> int:
        return val

    @staticmethod
    def echo_float(val: float) -> float:
        return val


class TestProtocolValidation:
    @pytest.mark.asyncio
    async def test_call_non_tool_method(self, protocol: Protocol) -> None:
        """Test calling a method that isn't from a Tool class"""

        def regular_function() -> str:
            return "not a tool"

        with pytest.raises(ValueError, match="Only methods of Tool classes can be called"):
            await protocol(regular_function)

    @pytest.mark.asyncio
    async def test_invalid_method_name(self, protocol: Protocol) -> None:
        """Test calling a method that doesn't exist on the tool"""
        from rmote.protocol import Flags, RPCRequest

        # First, ensure the tool is synced
        await protocol(ValidTool.method)

        # Directly craft a raw RPC request for a non-existent method to test remote validation.
        with pytest.raises(ValueError, match="not found"):
            await protocol._call(
                RPCRequest(method="ValidTool.nonexistent_method", args=(), kwargs={}),
                Flags.RPC | Flags.REQUEST,
            )


class TestProtocolEdgeCasesExtended:
    @pytest.mark.asyncio
    async def test_very_large_payload(self, protocol: Protocol) -> None:
        """Test handling very large payload (>10MB)"""
        # Create 10MB string
        large_data = "x" * (10 * 1024 * 1024)
        result = await protocol(LargePayloadTool.echo, large_data)
        assert len(result) == len(large_data)
        assert result == large_data

    @pytest.mark.asyncio
    async def test_deeply_nested_data(self, protocol: Protocol) -> None:
        """Test handling deeply nested data structures"""
        # Create deeply nested structure
        nested: dict[str, Any] = {"level": 0}
        current: dict[str, Any] = nested
        for i in range(1, 100):
            current["next"] = {"level": i}
            current = current["next"]

        result = await protocol(NestedDataTool.echo, nested)
        assert result["level"] == 0

        # Verify depth
        current = result
        for i in range(1, 100):
            current = current["next"]
            assert current["level"] == i

    @pytest.mark.asyncio
    async def test_special_characters_in_strings(self, protocol: Protocol) -> None:
        """Test strings with special characters"""
        special_strings = [
            "null\x00byte",
            "tab\there",
            "newline\nhere",
            'quote"here',
            "backslash\\here",
            "\r\n\t\x00\xff",
        ]

        for s in special_strings:
            result = await protocol(SpecialCharTool.echo, s)
            assert result == s

    @pytest.mark.asyncio
    async def test_tool_with_property_deleter(self) -> None:
        """Test Tool with property that has deleter"""

        class PropertyDeleterTool(Tool):
            _value: int = 42

            @property
            def value(self) -> int:
                return self._value

            @value.setter
            def value(self, val: int) -> None:
                self._value = val

            @value.deleter
            def value(self) -> None:
                self._value = 0

        from rmote.protocol import tool_from_dict, tool_to_dict

        tool_dict = tool_to_dict(PropertyDeleterTool)
        # Should include property source
        assert "def value" in tool_dict["source"]

        # Reconstruct
        restored = tool_from_dict(tool_dict)
        assert restored.__name__ == "PropertyDeleterTool"

    @pytest.mark.asyncio
    async def test_multiple_concurrent_exceptions(self, protocol: Protocol) -> None:
        """Test multiple concurrent requests that all raise exceptions"""
        tasks = [protocol(AlwaysFailTool.fail, f"Error {i}") for i in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should be exceptions
        assert all(isinstance(r, RuntimeError) for r in results)
        # Each should have unique message
        for i, result in enumerate(results):
            assert f"Error {i}" in str(result)

    @pytest.mark.asyncio
    async def test_empty_collections(self, protocol: Protocol) -> None:
        """Test empty collections"""
        assert await protocol(EmptyCollectionTool.echo_list, []) == []
        assert await protocol(EmptyCollectionTool.echo_dict, {}) == {}
        assert await protocol(EmptyCollectionTool.echo_set, set()) == set()

    @pytest.mark.asyncio
    async def test_tool_with_no_methods(self) -> None:
        """Test Tool with no methods (just class variables)"""

        class EmptyTool(Tool):
            config_value: int = 42

        from rmote.protocol import tool_from_dict, tool_to_dict

        tool_dict = tool_to_dict(EmptyTool)
        assert "config_value" in tool_dict["source"]

        restored = tool_from_dict(tool_dict)
        assert restored.__name__ == "EmptyTool"
        assert restored.config_value == 42  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_numeric_edge_cases(self, protocol: Protocol) -> None:
        """Test numeric edge cases"""
        # Very large integers
        assert await protocol(NumericTool.echo_int, 2**63 - 1) == 2**63 - 1
        assert await protocol(NumericTool.echo_int, -(2**63)) == -(2**63)

        # Float edge cases
        assert await protocol(NumericTool.echo_float, float("inf")) == float("inf")
        assert await protocol(NumericTool.echo_float, float("-inf")) == float("-inf")
        result = await protocol(NumericTool.echo_float, float("nan"))
        assert math.isnan(result)

        # Very small float
        assert await protocol(NumericTool.echo_float, 1e-100) == 1e-100
        # Very large float
        assert await protocol(NumericTool.echo_float, 1e100) == 1e100


class TestReadBoundary:
    """Unit tests for read_boundary - driven by StreamReader directly, no subprocess."""

    def _make_proto(self, data: bytes) -> BaseProtocol:
        from unittest.mock import MagicMock

        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return BaseProtocol(reader, MagicMock())

    @pytest.mark.asyncio
    async def test_finds_boundary_immediately(self) -> None:
        proto = self._make_proto(b"PROTOCOL READY\n")
        await proto.read_boundary()  # must not raise

    @pytest.mark.asyncio
    async def test_ignores_lines_before_boundary(self) -> None:
        proto = self._make_proto(b"garbage\njunk line\nPROTOCOL READY\n")
        await proto.read_boundary()  # must not raise

    @pytest.mark.asyncio
    async def test_large_chunk_without_newline_is_skipped(self) -> None:
        """128 KB of data without newline (exceeds default 64 KB limit) then boundary."""
        data = b"x" * 131072 + b"\nPROTOCOL READY\n"
        proto = self._make_proto(data)
        await proto.read_boundary()  # must not raise

    @pytest.mark.asyncio
    async def test_eof_before_boundary_raises(self) -> None:
        proto = self._make_proto(b"some garbage\n")
        with pytest.raises(ConnectionError, match="PROTOCOL READY"):
            await proto.read_boundary()

    @pytest.mark.asyncio
    async def test_empty_stream_raises(self) -> None:
        proto = self._make_proto(b"")
        with pytest.raises(ConnectionError, match="PROTOCOL READY"):
            await proto.read_boundary()


class TestDeadProcess:
    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_immediately_exiting_process_raises(self) -> None:
        """Protocol.from_subprocess on a process that exits immediately must raise, not hang."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "raise SystemExit(1)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proto = await Protocol.from_subprocess(process)
        with pytest.raises((ConnectionError, OSError)):
            async with proto:
                pass

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_empty_stdout_process_raises(self) -> None:
        """Process that closes stdout without writing PROTOCOL READY must raise, not hang."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stdout.close()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proto = await Protocol.from_subprocess(process)
        with pytest.raises((ConnectionError, OSError)):
            async with proto:
                pass
