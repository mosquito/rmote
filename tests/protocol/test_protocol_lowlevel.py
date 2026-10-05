"""Low-level protocol tests for error cases and edge coverage"""

import asyncio
import base64
import gzip
import pickle
import struct
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from rmote.protocol import (
    BaseProtocol,
    Flags,
    LogDelivery,
    Protocol,
    RemoteLogHandler,
    Tool,
    bootstrap_packer,
    bootstrap_payload,
    tool_from_dict,
    tool_to_dict,
)


class Packets(BaseProtocol):
    """Protocol that compresses each packet, without the frame codec."""

    FRAME_CODEC = False


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", [32, 2048])
    async def test_packet_encoding_and_roundtrip(self, size: int) -> None:
        writer = Mock(spec=asyncio.StreamWriter)
        writer.is_closing.return_value = False
        writer.drain = AsyncMock()
        reader = asyncio.StreamReader()
        # The gzip of a whole packet is the packet policy; the frame codec
        # compresses each frame instead, and test_frame_codec.py covers it.
        proto = Packets(reader, writer)
        packet = b"x" * size
        await proto.send(packet, Flags.RPC | Flags.REQUEST, 42)
        wire = writer.write.call_args.args[0]
        magic, flags, length, packet_id = proto.PACKET_HEADER.unpack(wire[: proto.PACKET_HEADER.size])
        payload = wire[proto.PACKET_HEADER.size :]
        assert (magic, length, packet_id) == (proto.MAGIC, len(payload), 42)
        if size > proto.COMPRESSION_THRESHOLD:
            assert flags == Flags.RPC | Flags.REQUEST | Flags.COMPRESSED
            assert payload.startswith(b"\x1f\x8b")
            assert gzip.decompress(payload) == pickle.dumps(packet)
        else:
            assert flags == Flags.RPC | Flags.REQUEST
            assert payload == pickle.dumps(packet)
        reader.feed_data(wire)
        reader.feed_eof()
        received = await proto.receive()
        assert (received.payload, received.flags, received.packet_id) == (packet, Flags(flags), 42)
        # The size is the serialized length before compression.
        assert received.size == len(pickle.dumps(packet))


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
    def test_bootstrap_packer_output(self, capsys) -> None:
        packed = bootstrap_packer(b"print('hello')")

        assert b"from gzip import decompress" in packed
        assert b"from base64 import b64decode" in packed
        assert b"exec(decompress(b64decode('''" in packed
        assert packed.startswith(b"from gzip import decompress\n")
        exec(packed, {})
        assert capsys.readouterr().out == "hello\n"

    def test_the_payload_is_built_once_for_the_process(self) -> None:
        first = bootstrap_payload()
        assert bootstrap_payload() is first
        assert bootstrap_payload.cache_info().misses == 1

    def test_the_payload_carries_the_protocol_source(self) -> None:
        encoded = bootstrap_payload().split(b"'''")[1]
        source = gzip.decompress(base64.b64decode(encoded))
        assert b"class Protocol" in source
        assert b"sys.modules['rmote.protocol']" in source

    @pytest.mark.asyncio
    async def test_every_connection_receives_the_same_bootstrap(self) -> None:
        written: list[bytes] = []
        original = asyncio.StreamWriter.write

        def watched(self: Any, data: Any) -> None:
            if bytes(data).startswith(b"from gzip import decompress"):
                written.append(bytes(data))
            original(self, data)

        asyncio.StreamWriter.write = watched  # type: ignore[method-assign]
        try:
            for _ in range(2):
                async with await Protocol.from_command(python=sys.executable) as remote:
                    assert remote is not None
        finally:
            asyncio.StreamWriter.write = original  # type: ignore[method-assign]
        assert len(written) == 2
        assert written[0] == written[1] == bootstrap_payload()


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

        log_payload = ("myapp", logging.INFO, "/remote/app.py", 42, "loop_log_sentinel ok", None)
        pickled = pickle.dumps([log_payload])
        header = BaseProtocol.PACKET_HEADER.pack(BaseProtocol.MAGIC, int(Flags.LOG), len(pickled), 0)

        reader = asyncio.StreamReader()
        reader.feed_data(header + pickled)
        reader.feed_eof()  # causes receive() to raise on the next read → _loop exits cleanly

        proto = Protocol(reader, _make_writer())

        with caplog.at_level(logging.INFO, logger="rmote.remote"):
            await proto._loop()
            # The worker of LogDelivery owns the handlers, so wait for it.
            await asyncio.to_thread(proto._logs.stop)

        assert "loop_log_sentinel ok" in caplog.text


class TestRunFunction:
    def test_run_is_async_entrypoint(self) -> None:
        import inspect

        from rmote.protocol import run

        assert inspect.iscoroutinefunction(run)


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

        # The sender formats the message, so the record carries the text.
        log_payload = ("myapp", logging.INFO, "/remote/app.py", 10, "hello world", None)
        with caplog.at_level(logging.INFO, logger="rmote.remote.myapp"):
            LogDelivery.emit(log_payload)
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

    def test_tool_to_dict_missing_module(self) -> None:
        """A class without an importable module uses the inline source path."""
        from unittest.mock import patch

        # _SourcelessTool is module-level so qualname doesn't contain "<locals>"
        with patch("rmote.protocol.inspect.getmodule", return_value=None):
            d = tool_to_dict(_SourcelessTool)
        assert d["name"] == "_SourcelessTool"
        assert "source" in d
