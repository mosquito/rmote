"""Pending RPC cleanup and packet integrity under cancellation and transport errors."""

import asyncio
import gc
import pickle
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from rmote.protocol import Flags, Protocol, Tool

REQUEST = Flags.RPC | Flags.REQUEST


@pytest_asyncio.fixture
async def client():
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = False
    writer.close.side_effect = lambda: setattr(writer.is_closing, "return_value", True)
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return Protocol(asyncio.StreamReader(), writer)


async def complete_call(client: Protocol, value: str = "ok") -> str:
    task = asyncio.create_task(client._call(value, REQUEST))
    await asyncio.sleep(0)
    assert len(client.futures) == 1
    packet_id = next(iter(client.futures))
    await client._handle_rpc_response(value, packet_id)
    result = await task
    assert isinstance(result, str)
    return result


@pytest.mark.asyncio
async def test_cancel_before_call_starts(client):
    task = asyncio.create_task(client._call("payload", REQUEST))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.futures
    client.writer.write.assert_not_called()
    assert await complete_call(client) == "ok"


@pytest.mark.asyncio
async def test_cancel_waiting_for_write_lock(client):
    async with client.write_lock:
        task = asyncio.create_task(client._call("payload", REQUEST))
        await asyncio.sleep(0)
        assert len(client.futures) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not client.futures
    client.writer.write.assert_not_called()
    client.writer.close.assert_not_called()
    assert await complete_call(client) == "ok"


@pytest.mark.asyncio
async def test_cancel_during_compression_keeps_channel(client, monkeypatch):
    entered = asyncio.Event()

    async def compress_in_thread(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("rmote.protocol.asyncio.to_thread", compress_in_thread)
    task = asyncio.create_task(client._call("x" * 4096, REQUEST))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.futures
    client.writer.write.assert_not_called()
    client.writer.close.assert_not_called()
    assert await complete_call(client) == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_response", [False, True])
async def test_cancel_waiting_for_response_ignores_late_packet(client, exception_response):
    task = asyncio.create_task(client._call("payload", REQUEST))
    await asyncio.sleep(0)
    packet_id = next(iter(client.futures))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.futures
    if exception_response:
        await client._handle_exception(ValueError("late"), packet_id)
    else:
        await client._handle_rpc_response("late", packet_id)
    client.writer.close.assert_not_called()
    assert await complete_call(client) == "ok"


@pytest.mark.asyncio
async def test_cancel_during_drain_closes_channel_and_pending_calls(client):
    draining = asyncio.Event()

    async def drain():
        draining.set()
        await asyncio.Event().wait()

    client.writer.drain.side_effect = drain
    task = asyncio.create_task(client._call("payload", REQUEST))
    await draining.wait()
    blocked = asyncio.create_task(client._call("blocked", REQUEST))
    await asyncio.sleep(0)
    assert len(client.futures) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ConnectionError):
        await blocked
    assert not client.futures
    client.writer.close.assert_called()
    with pytest.raises(ConnectionError):
        await client._call("new call", REQUEST)


@pytest.mark.asyncio
async def test_serialization_error_cleans_pending_but_keeps_channel(client):
    with pytest.raises((AttributeError, pickle.PicklingError)):
        await client._call(lambda: None, REQUEST)
    assert not client.futures
    client.writer.close.assert_not_called()
    assert await complete_call(client) == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "drain"])
async def test_send_error_preserves_original_exception(client, operation):
    error = BrokenPipeError("send failed")
    getattr(client.writer, operation).side_effect = error
    with pytest.raises(BrokenPipeError) as caught:
        await client._call("payload", REQUEST)
    assert caught.value is error
    assert not client.futures
    client.writer.close.assert_called_once()
    with pytest.raises(BrokenPipeError) as caught:
        await client._call("new call", REQUEST)
    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_response", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_response_to_completed_future_is_safe(client, exception_response, cancelled):
    future = client.loop.create_future()
    if cancelled:
        future.cancel()
    else:
        future.set_result("first result")
    client.futures[42] = future
    handler = client._handle_exception if exception_response else client._handle_rpc_response
    payload = ValueError("late") if exception_response else "late"
    await handler(payload, 42)
    await handler(payload, 42)  # A duplicate is an unknown packet.
    await handler(payload, 999)
    assert not client.futures
    if not cancelled:
        assert future.result() == "first result"
    assert await complete_call(client) == "ok"


@pytest.mark.asyncio
async def test_explicit_close_finishes_pending_without_receive_loop(client):
    tasks = [asyncio.create_task(client._call(i, REQUEST)) for i in range(3)]
    await asyncio.sleep(0)
    assert len(client.futures) == 3
    await client.__aexit__(None, None, None)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ConnectionError) for result in results)
    assert not client.futures
    with pytest.raises(ConnectionError, match="Connection closed"):
        await client._call("after close", REQUEST)
    await client.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_receive_error_finishes_all_pending_and_rejects_new_calls(client):
    tasks = [asyncio.create_task(client._call(i, REQUEST)) for i in range(3)]
    await asyncio.sleep(0)
    error = OSError("receive failed")
    client.reader.set_exception(error)
    await client._loop()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(result is error for result in results)
    assert not client.futures
    with pytest.raises(OSError) as caught:
        await client._call("after failure", REQUEST)
    assert caught.value is error


@pytest.mark.asyncio
async def test_eof_during_send_does_not_leave_unretrieved_future(client, caplog):
    draining = asyncio.Event()
    resume = asyncio.Event()

    async def drain():
        draining.set()
        await resume.wait()
        raise BrokenPipeError("send failed after EOF")

    client.writer.drain.side_effect = drain
    task = asyncio.create_task(client._call("payload", REQUEST))
    await draining.wait()
    client.reader.feed_eof()
    await client._loop()
    assert not client.futures
    resume.set()
    with pytest.raises(BrokenPipeError):
        await task
    with pytest.raises(asyncio.IncompleteReadError):
        await client._call("after EOF", REQUEST)
    del task
    gc.collect()
    assert "Future exception was never retrieved" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.timeout(10)
@pytest.mark.parametrize("fail", [False, True])
async def test_remote_operation_continues_after_local_cancellation(protocol, fail, caplog):
    class HeldTool(Tool):
        gate = None
        started = None
        finished = None

        @classmethod
        async def setup(cls):
            import asyncio

            cls.gate = asyncio.Event()
            cls.started = asyncio.Event()
            cls.finished = asyncio.Event()

        @classmethod
        async def wait(cls, fail):
            assert cls.started is not None
            assert cls.gate is not None
            assert cls.finished is not None
            cls.started.set()
            await cls.gate.wait()
            cls.finished.set()
            if fail:
                raise ValueError("late remote error")
            return "late remote result"

        @classmethod
        async def wait_started(cls):
            assert cls.started is not None
            await cls.started.wait()

        @classmethod
        async def wait_finished(cls):
            assert cls.finished is not None
            await cls.finished.wait()
            return "finished"

        @classmethod
        async def release(cls):
            assert cls.gate is not None
            cls.gate.set()

    await protocol(HeldTool.setup)
    task = asyncio.create_task(protocol(HeldTool.wait, fail))
    await protocol(HeldTool.wait_started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not protocol.futures
    await protocol(HeldTool.release)
    assert await protocol(HeldTool.wait_finished) == "finished"
    assert not protocol.futures
    assert not protocol._closed.is_set()
    assert "InvalidStateError" not in caplog.text


@pytest.mark.asyncio
async def test_cancel_first_tool_sync_can_retry(client, monkeypatch):
    class InlineTool(Tool):
        @staticmethod
        def value():
            return "ok"

    sent = asyncio.Event()
    original_send = client.send

    async def send(payload, flags, packet_id):
        await original_send(payload, flags, packet_id)
        if flags & Flags.SYNC:
            sent.set()

    monkeypatch.setattr(client, "send", send)
    task = asyncio.create_task(client(InlineTool.value))
    await sent.wait()
    packet_id = next(iter(client.futures))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert InlineTool not in client._tools_cache
    assert not client.futures
    await client._handle_rpc_response(None, packet_id)
    sent.clear()
    retry = asyncio.create_task(client(InlineTool.value))
    await sent.wait()
    await client._handle_rpc_response(None, next(iter(client.futures)))
    await asyncio.sleep(0)
    await client._handle_rpc_response("ok", next(iter(client.futures)))
    assert await retry == "ok"
    assert InlineTool in client._tools_cache
    assert not client.futures
