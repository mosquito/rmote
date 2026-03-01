"""Integration tests for built-in tools over the protocol"""

import tempfile
from pathlib import Path

import pytest

from rmote.protocol import Protocol, Tool
from rmote.tools import FileSystem, Logger


# Define custom tools at module level
class CustomDoubleTool(Tool):
    @staticmethod
    def double(x: int) -> int:
        return x * 2


class TestFileSystemIntegration:
    @pytest.mark.asyncio
    async def test_read_str_remote(self, protocol: Protocol) -> None:
        """Test reading a real file from remote"""
        # Create a temp file that the remote can read
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("Hello from remote!")
            temp_path = f.name

        try:
            content = await protocol(FileSystem.read_str, temp_path)
            assert content == "Hello from remote!"
        finally:
            Path(temp_path).unlink()

    @pytest.mark.asyncio
    async def test_read_bytes_remote(self, protocol: Protocol) -> None:
        """Test reading binary file from remote"""
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".bin") as f:
            data = b"\x00\x01\x02\xff\xfe"
            f.write(data)
            temp_path = f.name

        try:
            content = await protocol(FileSystem.read_bytes, temp_path)
            assert content == data
        finally:
            Path(temp_path).unlink()

    @pytest.mark.asyncio
    async def test_glob_remote(self, protocol: Protocol) -> None:
        """Test globbing files on remote"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            Path(tmpdir, "test1.txt").touch()
            Path(tmpdir, "test2.txt").touch()
            Path(tmpdir, "other.py").touch()

            files = await protocol(FileSystem.glob, tmpdir, "*.txt")
            assert len(files) == 2
            assert all(f.endswith(".txt") for f in files)

    @pytest.mark.asyncio
    async def test_read_nonexistent_file_remote(self, protocol: Protocol) -> None:
        """Test that reading nonexistent file raises FileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            await protocol(FileSystem.read_str, "/this/path/does/not/exist.txt")

    @pytest.mark.asyncio
    async def test_glob_empty_result(self, protocol: Protocol) -> None:
        """Test glob with no matches returns empty list"""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = await protocol(FileSystem.glob, tmpdir, "*.nonexistent")
            assert files == []

    @pytest.mark.asyncio
    async def test_concurrent_file_operations(self, protocol: Protocol) -> None:
        """Test concurrent file read operations"""
        import asyncio

        # Create multiple temp files
        temp_files = []
        for i in range(5):
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=f"_{i}.txt") as f:
                f.write(f"Content {i}")
                temp_files.append(f.name)

        try:
            # Read all files concurrently
            tasks = [protocol(FileSystem.read_str, path) for path in temp_files]
            results = await asyncio.gather(*tasks)

            # Verify results
            for i, content in enumerate(results):
                assert content == f"Content {i}"
        finally:
            for path in temp_files:
                Path(path).unlink()


class TestLoggerIntegration:
    @pytest.mark.asyncio
    async def test_set_log_level_remote(self, protocol: Protocol) -> None:
        """Test setting log level on remote"""
        # Should not raise any errors
        await protocol(Logger.set_log_level, "DEBUG")
        await protocol(Logger.set_log_level, "INFO")
        await protocol(Logger.set_log_level, "WARNING")

    @pytest.mark.asyncio
    async def test_invalid_log_level_remote(self, protocol: Protocol) -> None:
        """Test that invalid log level raises ValueError on remote"""
        with pytest.raises(ValueError, match="Invalid log level"):
            await protocol(Logger.set_log_level, "INVALID")

    @pytest.mark.asyncio
    async def test_log_level_boundary_values(self, protocol: Protocol) -> None:
        """Test boundary values for log levels"""
        # Valid boundary values
        await protocol(Logger.set_log_level, "NOTSET")
        await protocol(Logger.set_log_level, "CRITICAL")

        # Invalid boundary values
        with pytest.raises(ValueError):
            await protocol(Logger.set_log_level, "TRACE")

        with pytest.raises(ValueError):
            await protocol(Logger.set_log_level, "VERBOSE")


class TestMixedToolUsage:
    @pytest.mark.asyncio
    async def test_use_multiple_builtin_tools(self, protocol: Protocol) -> None:
        """Test using multiple built-in tools in sequence"""

        # Set log level
        await protocol(Logger.set_log_level, "INFO")

        # Create a temp file
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("test content")
            temp_path = f.name

        try:
            # Read the file
            content = await protocol(FileSystem.read_str, temp_path)
            assert content == "test content"

            # Glob for the file
            tmpdir = str(Path(temp_path).parent)
            files = await protocol(FileSystem.glob, tmpdir, "*.txt")
            assert any(temp_path in f for f in files)
        finally:
            Path(temp_path).unlink()

    @pytest.mark.asyncio
    async def test_interleave_builtin_and_custom_tools(self, protocol: Protocol) -> None:
        """Test interleaving built-in and custom tools"""
        # Interleave calls
        result1 = await protocol(CustomDoubleTool.double, 5)
        assert result1 == 10

        await protocol(Logger.set_log_level, "DEBUG")

        result2 = await protocol(CustomDoubleTool.double, 10)
        assert result2 == 20

        with tempfile.TemporaryDirectory() as tmpdir:
            files = await protocol(FileSystem.glob, tmpdir, "*")
            assert isinstance(files, list)

        result3 = await protocol(CustomDoubleTool.double, 15)
        assert result3 == 30
