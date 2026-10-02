"""Regression tests for rpc response serialization."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from rmote.protocol import Flags, Protocol, Tool
from rmote.sync import Connection

pytestmark = pytest.mark.timeout(10)


@pytest_asyncio.fixture
async def remote():
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proto = await Protocol.from_subprocess(child)
    try:
        await asyncio.wait_for(proto.__aenter__(), 3)
        yield proto
    finally:
        await asyncio.wait_for(proto.__aexit__(None, None, None), 3)
        if child.returncode is None:
            child.terminate()
        try:
            await asyncio.wait_for(child.wait(), 2)
        except TimeoutError:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
async def test_unpicklable_result_reports_error(remote):
    class Unpicklable(Tool):
        @staticmethod
        def value():
            return lambda: None

        @staticmethod
        def echo(value):
            return value

    assert await asyncio.wait_for(remote(Unpicklable.echo, 123), 2) == 123

    with pytest.raises(RuntimeError, match="Remote response serialization failed"):
        await asyncio.wait_for(remote(Unpicklable.value), 1)
    assert not remote.futures
    assert await remote(Unpicklable.echo, "still usable") == "still usable"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["async_value", "error", "unsafe_error_text"])
async def test_unpicklable_responses_preserve_connection(remote, method):
    class Unpicklable(Tool):
        @staticmethod
        async def async_value():
            return lambda: None

        @staticmethod
        def error():
            error = ValueError("error with unpicklable payload")
            error.__dict__["payload"] = lambda: None
            raise error

        @staticmethod
        def unsafe_error_text():
            class UnsafeError(Exception):
                def __str__(self):
                    raise RuntimeError("cannot format error")

            class Value:
                def __reduce__(self):
                    raise UnsafeError()

            return Value()

        @staticmethod
        def echo(value):
            return value

    with pytest.raises(RuntimeError, match="Remote response serialization failed"):
        await asyncio.wait_for(remote(getattr(Unpicklable, method)), 2)
    assert not remote.futures
    assert not remote._closed.is_set()
    assert await remote(Unpicklable.echo, "next RPC") == "next RPC"


@pytest.mark.asyncio
async def test_fallback_failure_closes_channel_and_finishes_pending(monkeypatch):
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = False
    proto = Protocol(asyncio.StreamReader(), writer)
    error = BrokenPipeError("fallback failed")
    send = AsyncMock(side_effect=[ValueError("cannot serialize"), error])
    monkeypatch.setattr(proto, "send", send)
    future = proto.loop.create_future()
    proto.futures[99] = future
    proto._execute(42, Flags.RPC, lambda payload, packet_id: None, None, True)
    tasks = list(proto._tasks)
    await asyncio.gather(*tasks)  # The wrapper must not have an unhandled error.
    await asyncio.sleep(0)
    writer.close.assert_called_once()
    assert not proto._tasks
    assert proto._closed.is_set()
    assert not proto.futures
    with pytest.raises(BrokenPipeError) as caught:
        await future
    assert caught.value is error
    assert send.await_count == 2
    assert send.await_args is not None
    fallback, flags, packet_id = send.await_args.args
    assert isinstance(fallback, RuntimeError)
    assert flags == Flags.EXCEPTION | Flags.RESPONSE
    assert packet_id == 42


@pytest.mark.asyncio
async def test_transport_failure_does_not_send_fallback(monkeypatch):
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = True
    proto = Protocol(asyncio.StreamReader(), writer)
    error = BrokenPipeError("send failed after write")
    send = AsyncMock(side_effect=error)
    monkeypatch.setattr(proto, "send", send)
    await proto._send_response("result", Flags.RPC | Flags.RESPONSE, 42)
    send.assert_awaited_once()
    assert proto._close_error is error
    assert proto._closed.is_set()


def test_sync_client_receives_serialization_error_and_can_continue():
    class Unpicklable(Tool):
        @staticmethod
        def value():
            return lambda: None

        @staticmethod
        def echo(value):
            return value

    with Connection.from_local() as remote:
        with pytest.raises(RuntimeError, match="Remote response serialization failed"):
            remote(Unpicklable.value)
        assert remote(Unpicklable.echo, "still usable") == "still usable"
