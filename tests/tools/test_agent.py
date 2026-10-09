"""Remote agent listeners: ownership, flow control, and cleanup."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from rmote.protocol import Protocol
from rmote.tools.agent import Agent


@pytest.mark.asyncio
async def test_remote_connections_are_session_owned_and_release_wakes_readers(protocol):
    first, second = uuid4().hex, uuid4().hex
    await protocol(Agent.start, first)
    await protocol(Agent.start, second)
    first_path = await protocol(Agent.listen, first)
    second_path = await protocol(Agent.listen, second)
    first_reader, first_writer = await asyncio.open_unix_connection(first_path)
    second_reader, second_writer = await asyncio.open_unix_connection(second_path)
    first_accept = protocol(Agent.accept, first).__aiter__()
    second_accept = protocol(Agent.accept, second).__aiter__()
    try:
        one, two = await anext(first_accept), await anext(second_accept)
        with pytest.raises(ConnectionError, match="Unknown agent connection"):
            await protocol(Agent.write, second, one, b"wrong session")
        first_output = protocol(Agent.output, first, one).__aiter__()
        first_writer.write(b"request")
        await first_writer.drain()
        assert await anext(first_output) == b"request"
        await protocol(Agent.write, first, one, b"response")
        assert await first_reader.readexactly(8) == b"response"
        waiting = asyncio.create_task(anext(first_output))
        await protocol(Agent.release, first)
        with pytest.raises(StopAsyncIteration):
            await waiting
        assert await first_reader.read() == b""
        assert not Path(first_path).parent.exists()
        # Releasing the first listener leaves the other connection alive.
        await protocol(Agent.write, second, two, b"alive")
        assert await second_reader.readexactly(5) == b"alive"
        await protocol(Agent.release, first)
    finally:
        await protocol(Agent.release, first)
        await protocol(Agent.release, second)
        await first_accept.aclose()
        await second_accept.aclose()
        first_writer.close()
        second_writer.close()
        await asyncio.gather(first_writer.wait_closed(), second_writer.wait_closed())
    assert not Path(second_path).parent.exists()


@pytest.mark.asyncio
async def test_agent_connection_limit_and_half_close(monkeypatch):
    monkeypatch.setattr(Agent, "MAX_CONNECTIONS", 2)
    session_id = uuid4().hex
    await Agent.start(session_id)
    path = await Agent.listen(session_id)
    clients = []
    accepting = Agent.accept(session_id)
    try:
        for _ in range(2):
            clients.append(await asyncio.open_unix_connection(path))
        one, two = await anext(accepting), await anext(accepting)
        extra_reader, extra_writer = await asyncio.open_unix_connection(path)
        clients.append((extra_reader, extra_writer))
        assert await asyncio.wait_for(extra_reader.read(), 2) == b""
        assert len(Agent._sessions[session_id].connections) == 2
        reader, writer = clients[0]
        writer.write(b"last request")
        writer.write_eof()
        chunks = [chunk async for chunk in Agent.output(session_id, one)]
        assert b"".join(chunks) == b"last request"
        await Agent.write(session_id, one, b"last response")
        await Agent.end_input(session_id, one)
        assert await reader.read() == b"last response"
        await Agent.close(session_id, one)
        with pytest.raises(ValueError, match="chunk limit"):
            await Agent.write(session_id, two, b"x" * (Agent.CHUNK_SIZE + 1))
    finally:
        await Agent.release(session_id)
        await accepting.aclose()
        for _, writer in clients:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for _, writer in clients))
    assert not Path(path).parent.exists()
    assert session_id not in Agent._sessions


@pytest.mark.asyncio
async def test_release_wakes_accept_without_connections():
    session_id = uuid4().hex
    await Agent.start(session_id)
    path = await Agent.listen(session_id)
    stream = Agent.accept(session_id)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await Agent.release(session_id)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 2)
    assert not Path(path).parent.exists()


@pytest.mark.asyncio
async def test_transport_eof_removes_abandoned_listener():
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with await Protocol.from_subprocess(process) as protocol:
            session_id = await protocol(Agent.start)
            path = await protocol(Agent.listen, session_id)
            assert Path(path).is_socket()
            # Closing the transport without a release RPC leaves only the
            # remote interpreter's normal EOF cleanup available.
        await asyncio.wait_for(process.wait(), 5)
        assert not Path(path).parent.exists()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
