"""SFTP framing, ordered requests, and resource cleanup at the mux boundary."""

import asyncio
import errno
import os
import socket
import struct
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest

from rmote.cli.sftp import MAX_PACKET, Message, Packet, SftpServer, Status, string
from rmote.cli.sshmux import MuxClient, MuxServer, SessionRequest
from rmote.tools.files import Files


def frame(kind: int, request: int, payload: bytes = b"") -> bytes:
    data = bytes((kind,)) + struct.pack(">I", request) + payload
    return struct.pack(">I", len(data)) + data


def open_request(path) -> bytes:
    return frame(Message.OPEN, 1, string(str(path)) + struct.pack(">II", 1 | 2 | 8, 0))


@pytest.mark.parametrize("flags", [0x10, 0x80000010])
def test_unknown_attribute_bits_are_rejected(flags):
    with pytest.raises(ValueError, match="Unknown attribute flags"):
        Packet(struct.pack(">I", flags)).attrs()


class Wire:
    def __init__(self, data: bytes = b"") -> None:
        self.input = asyncio.StreamReader()
        self.input.feed_data(data)
        self.output: list[bytes] = []
        self.sent: asyncio.Queue[bytes] = asyncio.Queue()

    async def write(self, data: bytes) -> None:
        self.output.append(data)
        await self.sent.put(data)

    async def response(self) -> tuple[int, int, Packet]:
        data = await asyncio.wait_for(self.sent.get(), 5)
        assert int.from_bytes(data[:4], "big") == len(data) - 4
        packet = Packet(data[4:])
        return packet.take(1)[0], packet.uint32(), packet


async def invoke(method: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(method, *args)


@pytest.mark.asyncio
async def test_pipelined_operations_and_error_statuses(tmp_path):
    wire = Wire(frame(Message.INIT, 3) + open_request(tmp_path / "file"))
    server = SftpServer(invoke, wire.input.read, wire.write)
    task = asyncio.create_task(server.run())
    try:
        assert (await wire.response())[:2] == (Message.VERSION, 3)
        kind, request, response = await wire.response()
        assert (kind, request) == (Message.HANDLE, 1)
        handle = response.string()
        wire.input.feed_data(
            frame(Message.WRITE, 2, string(handle) + struct.pack(">Q", 0) + string(b"abc"))
            + frame(Message.WRITE, 3, string(handle) + struct.pack(">Q", 1) + string(b"ZZ"))
            + frame(Message.READ, 4, string(handle) + struct.pack(">QI", 0, 3))
            + frame(Message.READ, 5, string(b"bogus") + struct.pack(">QI", 0, 3))
            + frame(Message.EXTENDED, 6, string("unknown-extension"))
            + frame(Message.CLOSE, 7, string(handle))
            + frame(Message.READ, 8, string(handle) + struct.pack(">QI", 0, 3))
        )
        for request in (2, 3):
            kind, identifier, result = await wire.response()
            assert (kind, identifier, result.uint32()) == (Message.STATUS, request, Status.OK)
        kind, identifier, result = await wire.response()
        assert (kind, identifier, result.string()) == (Message.DATA, 4, b"aZZ")
        for request, status in ((5, Status.FAILURE), (6, Status.OP_UNSUPPORTED), (7, Status.OK), (8, Status.FAILURE)):
            kind, identifier, result = await wire.response()
            assert (kind, identifier, result.uint32()) == (Message.STATUS, request, status)
        wire.input.feed_eof()
        assert await task == 0
        assert server.session_id not in Files._sessions
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"", b"\0\0", struct.pack(">I", MAX_PACKET + 1), frame(Message.WRITE, 2)[:-1]])
async def test_eof_and_bad_frames_close_open_descriptors(tmp_path, ending):
    wire = Wire(frame(Message.INIT, 3) + open_request(tmp_path / "file"))
    server = SftpServer(invoke, wire.input.read, wire.write)
    task = asyncio.create_task(server.run())
    await wire.response()
    _, _, result = await wire.response()
    handle = result.string()
    fd = Files._sessions[server.session_id].handles[handle].fd
    wire.input.feed_data(ending)
    wire.input.feed_eof()
    if ending:
        with pytest.raises(ValueError):
            await task
    else:
        assert await task == 0
    assert server.session_id not in Files._sessions
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


@pytest.mark.asyncio
async def test_cancelled_open_finishes_before_session_cleanup(tmp_path):
    opened, finish = asyncio.Event(), asyncio.Event()
    descriptors = []

    async def delayed(method, *args):
        result = method(*args)
        if method == Files.open:
            descriptors.append(Files._sessions[args[0]].handles[result].fd)
            opened.set()
            await finish.wait()
        return result

    wire = Wire(frame(Message.INIT, 3) + open_request(tmp_path / "file"))
    server = SftpServer(delayed, wire.input.read, wire.write)
    task = asyncio.create_task(server.run())
    await opened.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert server.session_id in Files._sessions
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.session_id not in Files._sessions
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.asyncio
async def test_mux_disconnect_closes_sftp_handles_even_with_open_stdin(tmp_path):
    control, peer = socket.socketpair()
    control.setblocking(False)
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    error_read, error_write = os.pipe()
    opened = asyncio.Event()
    descriptors = []
    tokens = []

    async def tracked(method, session_id, *args):
        result = await invoke(method, session_id, *args)
        if method == Files.open:
            tokens.append(session_id)
            descriptors.append(Files._sessions[session_id].handles[result].fd)
            opened.set()
        return result

    client = MuxClient(Mock(spec=MuxServer, protocol=tracked), control)
    client.request = SessionRequest(False, False, False, True, 0xFFFFFFFF, "", "sftp")
    client.fds = [input_read, output_write, error_write]
    task = asyncio.create_task(client.session(1))
    try:
        os.write(input_write, frame(Message.INIT, 3) + open_request(tmp_path / "file"))
        await asyncio.wait_for(opened.wait(), 5)
        peer.close()
        await asyncio.wait_for(task, 5)
        assert all(session_id not in Files._sessions for session_id in tokens)
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for flags in reversed(client.flags):
            flags.restore()
        control.close()
        peer.close()
        for fd in (input_read, input_write, output_read, output_write, error_read, error_write):
            os.close(fd)
