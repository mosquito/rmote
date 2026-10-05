import array
import asyncio
import os
import socket
import stat
import subprocess
import sys

import pytest

from rmote.cli.sshmux import MAX_PACKET, Message, MuxClient, MuxServer, Packet, SessionRequest, packet
from rmote.tools.vty import Vty
from tests.cli.sshmux.conftest import Server


def receive_packet(connection: socket.socket) -> Packet:
    def exact(size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            chunk = connection.recv(size - len(out))
            assert chunk
            out.extend(chunk)
        return bytes(out)

    size = int.from_bytes(exact(4), "big")
    return Packet(exact(size))


def test_fragmented_handshake_and_requests(server: Server):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(server.path))
        hello = receive_packet(connection)
        assert (hello.uint32(), hello.uint32()) == (Message.HELLO, 4)
        for byte in packet(Message.HELLO, 4, "unknown-extension", "ignored"):
            connection.sendall(bytes([byte]))
        for byte in packet(Message.ALIVE_CHECK, 321):
            connection.sendall(bytes([byte]))
        response = receive_packet(connection)
        assert (response.uint32(), response.uint32(), response.uint32()) == (
            Message.ALIVE,
            321,
            server.process.pid,
        )


@pytest.mark.parametrize(
    "payload",
    [b"\0\0", (MAX_PACKET + 1).to_bytes(4, "big"), packet(Message.HELLO, 99)],
    ids=["partial", "oversized", "version"],
)
def test_bad_client_does_not_break_server(server: Server, payload):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(server.path))
        receive_packet(connection)
        connection.sendall(payload)
    assert server.run("host", "printf alive").stdout == b"alive"


@pytest.mark.asyncio
async def test_bad_descriptor_message_does_not_leak_received_fds():
    # receive_fd does not use a Protocol; isolate its ancillary-message boundary.
    from unittest.mock import Mock

    first, second = socket.socketpair()
    first.setblocking(False)
    client = MuxClient(Mock(spec=MuxServer), first)
    try:
        with open(os.devnull, "rb") as stream:
            before = len(os.listdir("/dev/fd"))
            second.sendmsg([b"\0"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [stream.fileno()] * 2))])
            with pytest.raises(ValueError, match="one stdio descriptor"):
                await asyncio.wait_for(client.receive_fd(), 2)
            assert len(os.listdir("/dev/fd")) == before
            assert not client.fds
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_shutdown_during_open_collects_and_closes_the_new_session():
    from unittest.mock import Mock

    started, finish = asyncio.Event(), asyncio.Event()
    closed = []

    async def invoke(method, *args, **kwargs):
        if method is Vty.shell:
            started.set()
            await finish.wait()
            return 42
        assert method is Vty.close
        closed.append(args[0])
        return 0

    first, second = socket.socketpair()
    client = MuxClient(Mock(spec=MuxServer, protocol=invoke, launcher=""), first)
    client.request = SessionRequest(False, False, False, False, 0xFFFFFFFF, "", "cat")
    try:
        with open(os.devnull, "rb") as stream:
            client.fds = [stream.fileno()] * 3
            opening = asyncio.create_task(client.session(1))
            await started.wait()
            opening.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
            assert closed == [42]
    finally:
        first.close()
        second.close()


def test_existing_socket_is_not_replaced(server: Server):
    before = server.path.stat()
    result = subprocess.run(
        [sys.executable, "-m", "rmote", "sshmux", "--socket", str(server.path), "--python", sys.executable],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert server.path.stat().st_ino == before.st_ino
    assert stat.S_IMODE(before.st_mode) == 0o600
    assert server.run("host", "printf alive").stdout == b"alive"


def test_shutdown_does_not_unlink_a_replacement_file(server: Server):
    moved = server.path.with_name("original")
    server.path.rename(moved)
    server.path.write_text("keep")
    server.process.terminate()
    server.process.wait(timeout=10)
    assert server.path.read_text() == "keep"


def test_transport_failure_stops_listener(server: Server):
    # Vty execs the shell command. Its parent is the remote Python interpreter.
    result = server.run("host", "kill -KILL $PPID")
    assert result.returncode == 255
    assert server.process.wait(timeout=10) == 1
    assert not server.path.exists()
