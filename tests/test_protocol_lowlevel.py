"""Low-level protocol tests for error cases and edge coverage"""

import asyncio
import struct

import pytest

from rmote.protocol import (
    BaseProtocol,
    Flags,
    Protocol,
    RemoteLogHandler,
    Tool,
    bootstrap_packer,
    process,
    tool_from_dict,
    tool_to_dict,
)


class MockTransport:
    def is_closing(self) -> bool:
        return True


def _make_writer() -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    return asyncio.StreamWriter(
        transport=MockTransport(),  # type: ignore[arg-type]
        protocol=asyncio.StreamReaderProtocol(asyncio.StreamReader()),
        reader=None,
        loop=loop,
    )


class TestProtocolLowLevel:
    @pytest.mark.asyncio
    async def test_invalid_magic_number(self) -> None:
        data = struct.pack(">5sIIQ", b"WRONG", 0, 10, 1) + b"test" + b"\x00" * 6
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        proto = BaseProtocol(reader, _make_writer())

        with pytest.raises(ValueError, match="Invalid magic number"):
            await proto.receive()

    @pytest.mark.asyncio
    async def test_send_with_compression_flag_raises(self) -> None:
        proto = BaseProtocol(asyncio.StreamReader(), _make_writer())

        with pytest.raises(ValueError, match="Compression flag must not be set"):
            await proto.send({"test": "data"}, Flags.COMPRESSED, 1)


class TestToolSerializationEdgeCases:
    def test_tool_from_dict_with_bases(self) -> None:
        class BaseTool(Tool):
            @staticmethod
            def base_method() -> str:
                return "base"

        class DerivedTool(BaseTool):
            @staticmethod
            def derived_method() -> str:
                return "derived"

        tool_dict = tool_to_dict(DerivedTool)
        assert "BaseTool" in tool_dict["source"]

        context = {"BaseTool": BaseTool}
        restored = tool_from_dict(tool_dict, context)

        assert restored.__name__ == "DerivedTool"
        instance = restored()
        assert instance.derived_method() == "derived"  # type: ignore[attr-defined]

    def test_tool_from_dict_with_class_vars_no_annotation(self) -> None:
        class ConfigTool(Tool):
            max_retries = 5
            timeout: int = 30

        restored = tool_from_dict(tool_to_dict(ConfigTool))
        assert restored.__name__ == "ConfigTool"

    def test_inline_tool_dict_shape(self) -> None:
        class InlineTool(Tool):
            @staticmethod
            def greet() -> str:
                return "hello"

        tool_dict = tool_to_dict(InlineTool)

        assert tool_dict["name"] == "InlineTool"
        assert "def greet" in tool_dict["source"]
        assert "module" not in tool_dict


class TestBootstrapPacker:
    def test_bootstrap_packer_output(self) -> None:
        packed = bootstrap_packer(b"print('hello')")

        assert b"from lzma import decompress" in packed
        assert b"from base64 import b64decode" in packed
        assert b"exec(decompress(b64decode('''" in packed
        assert packed.startswith(b"from lzma import decompress\n")


class TestHighLevelProtocolEdgeCases:
    @pytest.mark.asyncio
    async def test_protocol_context_manager_cleanup(self, protocol: Protocol) -> None:
        class SlowTool(Tool):
            @staticmethod
            async def slow() -> str:
                await asyncio.sleep(0.1)
                return "done"

        _task = asyncio.create_task(protocol(SlowTool.slow))  # noqa: F841
        # exiting the protocol context (handled by fixture) cancels pending tasks

    @pytest.mark.asyncio
    async def test_call_non_tool_method_raises(self, protocol: Protocol) -> None:
        def regular_function() -> str:
            return "not a tool"

        with pytest.raises(ValueError, match="Only methods of Tool classes"):
            await protocol(regular_function)

    @pytest.mark.asyncio
    async def test_loop_dispatches_log_packet(self, caplog) -> None:
        """_loop routes a LOG packet through _handle_log (covers lines 658-659, 594-604)."""
        import logging
        import pickle

        log_payload = {
            "name": "myapp",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "pathname": "/remote/app.py",
            "lineno": 42,
            "msg": "loop_log_sentinel %s",
            "args": ("ok",),
            "exc_info": None,
        }
        pickled = pickle.dumps(log_payload)
        header = BaseProtocol.PACKET_HEADER.pack(BaseProtocol.MAGIC, int(Flags.LOG), len(pickled), 0)

        reader = asyncio.StreamReader()
        reader.feed_data(header + pickled)
        reader.feed_eof()  # causes receive() to raise on the next read → _loop exits cleanly

        proto = Protocol(reader, _make_writer())

        with caplog.at_level(logging.INFO, logger="rmote.remote"):
            await proto._loop()
            # Let wrapper task and _handle_log task drain
            await asyncio.sleep(0.01)

        assert "loop_log_sentinel ok" in caplog.text


class TestRunFunction:
    def test_run_is_async_entrypoint(self) -> None:
        import inspect

        from rmote.protocol import run

        assert inspect.iscoroutinefunction(run)

    def test_process_is_sync_subprocess_helper(self) -> None:
        import inspect

        from rmote.protocol import process

        assert not inspect.iscoroutinefunction(process)
        result = process("echo", "hello", capture_output=True, text=True)
        assert result.returncode == 0
        assert "hello" in result.stdout


# ---------------------------------------------------------------------------
# process() unit tests
# ---------------------------------------------------------------------------


class TestProcessFunction:
    def test_basic_no_capture(self) -> None:
        """capture_output=False (default) sets stdout/stderr to DEVNULL."""
        result = process("true", capture_output=False)
        assert result.returncode == 0

    def test_capture_output(self) -> None:
        result = process("echo", "hello", capture_output=True, text=True)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_stdin_bytes(self) -> None:
        result = process("cat", stdin=b"hello", capture_output=True)
        assert result.stdout == b"hello"

    def test_stdin_str_is_encoded(self) -> None:
        """str stdin is converted to bytes before passing to subprocess."""
        result = process("cat", stdin="world", capture_output=True)
        assert result.stdout == b"world"

    def test_check_raises(self) -> None:
        import subprocess

        with pytest.raises(subprocess.CalledProcessError):
            process("false", check=True)

    def test_cwd(self, tmp_path) -> None:
        result = process("pwd", capture_output=True, text=True, cwd=str(tmp_path))
        assert str(tmp_path) in result.stdout

    def test_env(self) -> None:
        result = process(
            "sh",
            "-c",
            "echo $RMOTE_TEST_VAR",
            env={"RMOTE_TEST_VAR": "sentinel", "PATH": "/bin:/usr/bin"},
            capture_output=True,
            text=True,
        )
        assert "sentinel" in result.stdout


# ---------------------------------------------------------------------------
# Protocol unit tests (local, no subprocess)
# ---------------------------------------------------------------------------

_ModuleLevelTool: type  # forward ref for tool_to_dict except test


class _SourcelessTool(Tool):
    """Defined at module level so tool_to_dict takes the file path."""

    pass


class TestProtocolInternals:
    @pytest.mark.asyncio
    async def test_wait_closed(self) -> None:
        proto = Protocol(asyncio.StreamReader(), _make_writer())
        proto._closed.set()
        await proto.wait_closed()  # returns immediately - event already set

    @pytest.mark.asyncio
    async def test_handle_rpc_response_unknown_packet(self) -> None:
        """Orphan response (no matching future) logs a warning and does nothing."""
        proto = Protocol(asyncio.StreamReader(), _make_writer())
        # No futures registered → should just log and return
        await proto._handle_rpc_response("result", packet_id=99999)

    @pytest.mark.asyncio
    async def test_handle_exception_unknown_packet(self) -> None:
        """Orphan exception response logs a warning and does nothing."""
        proto = Protocol(asyncio.StreamReader(), _make_writer())
        await proto._handle_exception(RuntimeError("orphan"), packet_id=99999)

    @pytest.mark.asyncio
    async def test_handle_log(self, caplog) -> None:
        """_handle_log reconstructs and dispatches a LogRecord."""
        import logging

        log_payload = {
            "name": "myapp",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "pathname": "/remote/app.py",
            "lineno": 10,
            "msg": "hello %s",
            "args": ("world",),
            "exc_info": None,
        }
        with caplog.at_level(logging.INFO, logger="rmote.remote.myapp"):
            await Protocol._handle_log(log_payload, 0)  # type: ignore[arg-type]
        assert "hello world" in caplog.text

    @pytest.mark.asyncio
    async def test_remote_log_handler_emit(self) -> None:
        """RemoteLogHandler.__init__ and emit() create a send task without crashing."""
        import logging

        proto = Protocol(asyncio.StreamReader(), _make_writer())
        loop = asyncio.get_running_loop()
        handler = RemoteLogHandler(proto, loop)
        assert handler.protocol is proto

        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="<test>",
            lineno=0,
            msg="test %s",
            args=("msg",),
            exc_info=None,
        )
        # emit() creates an asyncio task - we just verify it doesn't raise
        handler.emit(record)
        # drain pending tasks to avoid "task was destroyed but pending!" warnings
        await asyncio.sleep(0)

    def test_tool_to_dict_getfile_exception(self) -> None:
        """tool_to_dict falls back to __source__ when inspect.getfile raises."""
        from unittest.mock import patch

        # _SourcelessTool is module-level so qualname doesn't contain "<locals>"
        with patch("rmote.protocol.inspect.getfile", side_effect=OSError("no file")):
            d = tool_to_dict(_SourcelessTool)
        assert d["name"] == "_SourcelessTool"
        assert "source" in d
