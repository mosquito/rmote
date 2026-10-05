"""Traceback text crosses the wire and survives local logging formatting."""

import asyncio
import logging
import sys
import threading
from typing import cast

import pytest

from rmote.protocol import Flags, LogRecord, Protocol, RemoteLogHandler, Tool
from rmote.sync import Connection

pytestmark = pytest.mark.timeout(10)


class BufferWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def is_closing(self):
        return False


async def serialize_record(
    record: logging.LogRecord, monkeypatch: pytest.MonkeyPatch, formatter: logging.Formatter | None = None
) -> LogRecord:
    loop = asyncio.get_running_loop()
    writer = BufferWriter()
    proto = Protocol(asyncio.StreamReader(), cast(asyncio.StreamWriter, writer))
    result: asyncio.Future[LogRecord] = loop.create_future()
    original_send = proto.send

    async def capture(packet, flags, packet_id):
        try:
            await original_send(packet, flags, packet_id)
        except Exception as exc:
            result.set_exception(exc)
        else:
            result.set_result(packet)

    monkeypatch.setattr(proto, "send", capture)
    handler = RemoteLogHandler(proto, loop)
    handler.setFormatter(formatter)
    handler.emit(record)
    packet = await asyncio.wait_for(result, 1)
    assert writer.data
    return packet


def exception_record() -> logging.LogRecord:
    try:
        raise ValueError("traceback detail")
    except ValueError:
        return logging.LogRecord(
            "exception-test", logging.ERROR, __file__, 1, "failed %s", ("operation",), sys.exc_info()
        )


@pytest.mark.asyncio
async def test_exception_log_is_serializable(monkeypatch):
    record = exception_record()
    exc_info = record.exc_info
    packet = await serialize_record(record, monkeypatch)
    assert packet["msg"] == "failed operation"
    assert packet["args"] == ()
    assert packet["exc_info"] is None
    assert "ValueError: traceback detail" in (packet["exc_text"] or "")
    assert record.exc_info is exc_info
    assert record.exc_text is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_exception_formatter_and_cached_text_are_preserved(monkeypatch, cached):
    class Formatter(logging.Formatter):
        def formatException(self, exc_info):
            return "custom exception text"

    record = exception_record()
    if cached:
        record.exc_text = "cached exception text"
    packet = await serialize_record(record, monkeypatch, Formatter())
    assert packet["exc_text"] == ("cached exception text" if cached else "custom exception text")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_receiver_formats_traceback_text_and_accepts_legacy_records(monkeypatch, legacy):
    packet = await serialize_record(exception_record(), monkeypatch)
    text = packet.get("exc_text")
    if legacy:
        del packet["exc_text"]
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("rmote.remote.exception-test")
    monkeypatch.setattr(logger, "handle", records.append)
    await Protocol._handle_log(packet, 1)
    received = records[0]
    assert received.exc_info is None
    assert received.exc_text == (None if legacy else text)
    formatted = logging.Formatter("%(message)s").format(received)
    assert formatted.startswith("failed operation")
    assert formatted.count("ValueError: traceback detail") == (0 if legacy else 1)


@pytest.mark.asyncio
async def test_plain_log_has_no_exception_text(monkeypatch):
    record = logging.LogRecord("plain", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    packet = await serialize_record(record, monkeypatch)
    assert packet["msg"] == "hello world"
    assert packet["exc_info"] is None
    assert packet["exc_text"] is None


@pytest.mark.asyncio
async def test_send_failure_reaches_handler_error_callback(monkeypatch):
    loop = asyncio.get_running_loop()
    proto = Protocol(asyncio.StreamReader(), cast(asyncio.StreamWriter, BufferWriter()))
    handler = RemoteLogHandler(proto, loop)
    failure = OSError("log transport failed")
    reported = loop.create_future()
    record = exception_record()

    async def fail_send(packet, flags, packet_id):
        assert flags == Flags.LOG
        raise failure

    def capture_error(original_record):
        reported.set_result((original_record, sys.exc_info()[1]))

    monkeypatch.setattr(proto, "send", fail_send)
    monkeypatch.setattr(handler, "handleError", capture_error)
    handler.emit(record)
    assert await asyncio.wait_for(reported, 1) == (record, failure)


@pytest.mark.parametrize("method", ["sync_log", "async_log"])
def test_remote_tool_exception_log_preserves_chained_traceback(method):
    class ExceptionLogger(Tool):
        @staticmethod
        def sync_log() -> str:
            import logging

            try:
                try:
                    raise ValueError("root cause")
                except ValueError as cause:
                    error = RuntimeError("outer error")
                    error.__dict__["payload"] = lambda: None
                    raise error from cause
            except RuntimeError:
                logging.getLogger("tool-exception-test").exception("tool failed %s", "operation")
            return "complete"

        @classmethod
        async def async_log(cls) -> str:
            cls.sync_log()
            return "complete"

    records = []
    delivered = threading.Event()

    class Capture(logging.Handler):
        def emit(self, record):
            records.append((record, self.format(record)))
            delivered.set()

    logger = logging.getLogger("rmote.remote.tool-exception-test")
    handler = Capture()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)
    try:
        with Connection.from_local() as remote:
            assert remote(getattr(ExceptionLogger, method)) == "complete"
            assert delivered.wait(2)
        record, formatted = records[0]
        assert record.exc_info is None
        assert formatted.startswith("ERROR: tool failed operation")
        assert "ValueError: root cause" in formatted
        assert "direct cause" in formatted
        assert "RuntimeError: outer error" in formatted
        assert formatted.count("Traceback (most recent call last)") == 2
    finally:
        logger.removeHandler(handler)
