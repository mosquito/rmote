"""Advanced protocol tests - errors, compression, edge cases"""

import asyncio
from typing import Any

import pytest

from rmote.protocol import Protocol, Tool
from rmote.tools import FileSystem, Logger


# Define tools at module level (not inside test functions) to avoid qualname issues
class ErrorTool(Tool):
    @staticmethod
    def raise_error() -> None:
        raise ValueError("Remote error!")


class AsyncErrorTool(Tool):
    @staticmethod
    async def async_error() -> None:
        await asyncio.sleep(0.001)
        raise RuntimeError("Async error!")


class MixedTool(Tool):
    @staticmethod
    def success(value: int) -> int:
        return value * 2

    @staticmethod
    def failure() -> None:
        raise ValueError("Failure!")


class LargeDataTool(Tool):
    @staticmethod
    def create_large_string() -> str:
        # Create string > 1024 bytes (compression threshold)
        return "x" * 2000

    @staticmethod
    def echo_large(data: str) -> str:
        return data


class ThresholdTool(Tool):
    @staticmethod
    def echo(data: str) -> str:
        return data


class ConcurrentTool(Tool):
    @staticmethod
    async def slow_double(value: int) -> int:
        await asyncio.sleep(0.01)
        return value * 2


class Tool1(Tool):
    @staticmethod
    def process(x: int) -> int:
        return x + 1


class Tool2(Tool):
    @staticmethod
    def process(x: int) -> int:
        return x * 2


class Tool3(Tool):
    @staticmethod
    def process(x: int) -> int:
        return x - 1


class MixedMethodsTool(Tool):
    @staticmethod
    def sync_method(value: int) -> int:
        return value * 2

    @staticmethod
    async def async_method(value: int) -> int:
        await asyncio.sleep(0.01)
        return value * 3


class EchoTool(Tool):
    @staticmethod
    def echo(data: str) -> str:
        return data


class NoneTool(Tool):
    @staticmethod
    def get_none() -> None:
        return None

    @staticmethod
    def echo(value: int | None) -> int | None:
        return value


class ComplexTool(Tool):
    @staticmethod
    def echo(data: dict[str, Any]) -> dict[str, Any]:
        return data


class BinaryTool(Tool):
    @staticmethod
    def echo_bytes(data: bytes) -> bytes:
        return data


class UnicodeTool(Tool):
    @staticmethod
    def echo(text: str) -> str:
        return text


class CachedTool(Tool):
    @staticmethod
    def method1() -> str:
        return "m1"

    @staticmethod
    def method2() -> str:
        return "m2"


class ReturnTypesTool(Tool):
    @staticmethod
    def return_int() -> int:
        return 42

    @staticmethod
    def return_float() -> float:
        return 3.14

    @staticmethod
    def return_list() -> list[int]:
        return [1, 2, 3]

    @staticmethod
    def return_dict() -> dict[str, int]:
        return {"a": 1, "b": 2}

    @staticmethod
    def return_bool() -> bool:
        return True


class KwargsTool(Tool):
    @staticmethod
    def method(a: int, b: int = 10, c: int = 20) -> int:
        return a + b + c


class VarargsTool(Tool):
    @staticmethod
    def sum_all(*args: int) -> int:
        return sum(args)


class TestProtocolErrors:
    @pytest.mark.asyncio
    async def test_remote_exception_propagates(self, protocol: Protocol) -> None:
        """Test that exceptions from remote side are propagated to local"""
        with pytest.raises(ValueError, match="Remote error!"):
            await protocol(ErrorTool.raise_error)

    @pytest.mark.asyncio
    async def test_file_not_found_exception(self, protocol: Protocol) -> None:
        """Test FileNotFoundError from remote"""
        with pytest.raises(FileNotFoundError):
            await protocol(FileSystem.read_str, "/nonexistent/path/file.txt")

    @pytest.mark.asyncio
    async def test_invalid_log_level(self, protocol: Protocol) -> None:
        """Test invalid log level raises ValueError"""
        with pytest.raises(ValueError, match="Invalid log level"):
            await protocol(Logger.set_log_level, "INVALID")

    @pytest.mark.asyncio
    async def test_async_exception(self, protocol: Protocol) -> None:
        """Test exception from async method"""
        with pytest.raises(RuntimeError, match="Async error!"):
            await protocol(AsyncErrorTool.async_error)

    @pytest.mark.asyncio
    async def test_exception_with_concurrent_requests(self, protocol: Protocol) -> None:
        """Test that one exception doesn't affect other requests"""
        # Mix successful and failing requests
        tasks = [
            protocol(MixedTool.success, 1),
            protocol(MixedTool.failure),
            protocol(MixedTool.success, 2),
            protocol(MixedTool.failure),
            protocol(MixedTool.success, 3),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check results
        assert results[0] == 2
        assert isinstance(results[1], ValueError)
        assert results[2] == 4
        assert isinstance(results[3], ValueError)
        assert results[4] == 6


class TestProtocolCompression:
    @pytest.mark.asyncio
    async def test_large_data_compression(self, protocol: Protocol) -> None:
        """Test that large data is compressed automatically"""
        # Get large data from remote (should be compressed in response)
        result = await protocol(LargeDataTool.create_large_string)
        assert len(result) == 2000
        assert result == "x" * 2000

        # Send large data to remote (should be compressed in request)
        large_input = "y" * 3000
        result = await protocol(LargeDataTool.echo_large, large_input)
        assert result == large_input

    @pytest.mark.asyncio
    async def test_compression_threshold(self, protocol: Protocol) -> None:
        """Test data just below and above compression threshold"""
        # Just below threshold (1024 bytes) - not compressed
        small_data = "a" * 1000
        result = await protocol(ThresholdTool.echo, small_data)
        assert result == small_data

        # Just above threshold - compressed
        large_data = "b" * 1500
        result = await protocol(ThresholdTool.echo, large_data)
        assert result == large_data


class TestProtocolConcurrency:
    @pytest.mark.asyncio
    async def test_many_concurrent_requests(self, protocol: Protocol) -> None:
        """Test handling many concurrent requests"""
        # Launch 50 concurrent requests
        tasks = [protocol(ConcurrentTool.slow_double, i) for i in range(50)]
        results = await asyncio.gather(*tasks)

        # Verify all results
        assert results == [i * 2 for i in range(50)]

    @pytest.mark.asyncio
    async def test_interleaved_tool_usage(self, protocol: Protocol) -> None:
        """Test using multiple tools in interleaved fashion"""
        # Interleave calls to different tools
        tasks = []
        for i in range(10):
            tasks.append(protocol(Tool1.process, i))
            tasks.append(protocol(Tool2.process, i))
            tasks.append(protocol(Tool3.process, i))

        results = await asyncio.gather(*tasks)

        # Verify results
        for i in range(10):
            assert results[i * 3] == i + 1  # Tool1
            assert results[i * 3 + 1] == i * 2  # Tool2
            assert results[i * 3 + 2] == i - 1  # Tool3

    @pytest.mark.asyncio
    async def test_sync_and_async_methods_concurrent(self, protocol: Protocol) -> None:
        """Test concurrent calls to both sync and async methods"""
        tasks = []
        for i in range(20):
            if i % 2 == 0:
                tasks.append(protocol(MixedMethodsTool.sync_method, i))
            else:
                tasks.append(protocol(MixedMethodsTool.async_method, i))

        results = await asyncio.gather(*tasks)

        for i in range(20):
            if i % 2 == 0:
                assert results[i] == i * 2
            else:
                assert results[i] == i * 3


class TestProtocolEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_string(self, protocol: Protocol) -> None:
        """Test handling empty string"""
        result = await protocol(EchoTool.echo, "")
        assert result == ""

    @pytest.mark.asyncio
    async def test_none_value(self, protocol: Protocol) -> None:
        """Test handling None values"""
        assert await protocol(NoneTool.get_none) is None  # type: ignore[func-returns-value]

        result = await protocol(NoneTool.echo, None)
        assert result is None

        result = await protocol(NoneTool.echo, 42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_complex_data_structures(self, protocol: Protocol) -> None:
        """Test handling complex nested data structures"""
        complex_data = {
            "list": [1, 2, 3, [4, 5]],
            "dict": {"nested": {"key": "value"}},
            "tuple": (1, 2, 3),
            "set": {1, 2, 3},
            "none": None,
            "bool": True,
        }

        result = await protocol(ComplexTool.echo, complex_data)
        assert result["list"] == [1, 2, 3, [4, 5]]
        assert result["dict"] == {"nested": {"key": "value"}}
        assert result["tuple"] == (1, 2, 3)
        assert result["set"] == {1, 2, 3}
        assert result["none"] is None
        assert result["bool"] is True

    @pytest.mark.asyncio
    async def test_binary_data(self, protocol: Protocol) -> None:
        """Test handling binary data"""
        binary_data = b"\x00\x01\x02\xff\xfe\xfd"
        result = await protocol(BinaryTool.echo_bytes, binary_data)
        assert result == binary_data

    @pytest.mark.asyncio
    async def test_unicode_data(self, protocol: Protocol) -> None:
        """Test handling unicode strings"""
        unicode_text = "Hello 世界 🌍 مرحبا Здравствуй"
        result = await protocol(UnicodeTool.echo, unicode_text)
        assert result == unicode_text

    @pytest.mark.asyncio
    async def test_tool_caching(self, protocol: Protocol) -> None:
        """Test that tools are only synced once (cached)"""
        # First call - tool should be synced
        result1 = await protocol(CachedTool.method1)
        assert result1 == "m1"

        # Second call to different method - should use cached tool
        result2 = await protocol(CachedTool.method2)
        assert result2 == "m2"

        # Third call to first method - still cached
        result3 = await protocol(CachedTool.method1)
        assert result3 == "m1"

    @pytest.mark.asyncio
    async def test_return_types(self, protocol: Protocol) -> None:
        """Test various return types"""
        assert await protocol(ReturnTypesTool.return_int) == 42
        assert await protocol(ReturnTypesTool.return_float) == 3.14
        assert await protocol(ReturnTypesTool.return_list) == [1, 2, 3]
        assert await protocol(ReturnTypesTool.return_dict) == {"a": 1, "b": 2}
        assert await protocol(ReturnTypesTool.return_bool) is True

    @pytest.mark.asyncio
    async def test_kwargs(self, protocol: Protocol) -> None:
        """Test methods with keyword arguments"""
        # Positional only
        assert await protocol(KwargsTool.method, 1) == 31

        # Mix positional and keyword
        assert await protocol(KwargsTool.method, 1, b=5) == 26

        # All keyword
        result = await protocol(KwargsTool.method, a=1, b=2, c=3)
        assert result == 6

    @pytest.mark.asyncio
    async def test_varargs(self, protocol: Protocol) -> None:
        """Test methods with *args"""
        assert await protocol(VarargsTool.sum_all, 1, 2, 3) == 6
        assert await protocol(VarargsTool.sum_all, 10, 20, 30, 40) == 100
        assert await protocol(VarargsTool.sum_all) == 0
