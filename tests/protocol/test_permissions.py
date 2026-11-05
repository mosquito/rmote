"""Peer permissions reject sources and calls without stopping the connection."""

import asyncio
import pickle
import socket
import sys
from contextlib import asynccontextmanager
from typing import Any

import pytest

from rmote.protocol import (
    BaseProtocol,
    Flags,
    ModuleSource,
    PacketUnpickler,
    Protocol,
    Tool,
    tool_from_dict,
    tool_to_dict,
)
from tests.support.tool_cases.permissions import Permissions, Ping


@asynccontextmanager
async def connected(*, threshold=-1):
    first, second = socket.socketpair()
    left_stream, right_stream = await asyncio.gather(
        asyncio.open_connection(sock=first), asyncio.open_connection(sock=second)
    )
    left = Protocol(*left_stream, decode_threshold=threshold)
    right = Protocol(*right_stream, accept_code=True, accept_calls=True, decode_threshold=threshold)
    left.tools["Ping"] = right.tools["Ping"] = Ping()
    try:
        await asyncio.gather(left.__aenter__(), right.__aenter__())
        yield left, right
    finally:
        await asyncio.gather(left.__aexit__(None, None, None), right.__aexit__(None, None, None))


async def ping(sender: Protocol) -> Any:
    return await sender._call({"method": "Ping.echo", "args": (), "kwargs": {}}, Flags.RPC | Flags.REQUEST)


@pytest.mark.asyncio
async def test_calls_and_code_require_separate_permissions():
    async with connected() as (left, right):
        assert not left.accept_code and not left.accept_calls
        with pytest.raises(PermissionError):
            await ping(right)
        assert await ping(left) == 42
        left.accept_calls = True
        assert await ping(right) == 42
        definition = tool_to_dict(Ping, {Ping.__module__})
        with pytest.raises(PermissionError):
            await right._call(definition, Flags.SYNC | Flags.REQUEST)
        left.accept_code = True
        await right._call(definition, Flags.SYNC | Flags.REQUEST)
        left.accept_calls = False
        with pytest.raises(PermissionError):
            await ping(right)
        right.accept_calls = False
        with pytest.raises(PermissionError):
            await ping(left)
        right.accept_calls = True
        assert await ping(left) == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [-1, 1], ids=["inline", "worker"])
@pytest.mark.parametrize("stream", [False, True], ids=["result", "stream"])
async def test_module_response_is_rejected_before_unpickling(tmp_path, threshold, stream):
    marker = tmp_path / "executed"
    name = "forbidden_peer_module"
    sources = {
        name: ModuleSource(
            source=f"open({str(marker)!r}, 'w').close()\n" + "# padding\n" * 30000,
            file=f"<{name}>",
            package=False,
        )
    }

    class Body:
        def __reduce__(self):
            return eval, (f"open({str(marker)!r}, 'w').close()",)

    body = pickle.dumps(Body())
    flags = Flags.RPC | Flags.RESPONSE | Flags.MODULES
    if stream:
        body = BaseProtocol.batch([body])
        flags |= Flags.STREAM | Flags.BATCH
    payload = pickle.dumps((sources, body))
    async with connected(threshold=threshold) as (left, right):
        meta_path = sys.meta_path.copy()
        modules = set(sys.modules)
        packet_id = left.get_id()
        if stream:
            queue: asyncio.Queue[tuple[Flags, Any, int]] = asyncio.Queue()
            left.streams[packet_id] = queue
        else:
            future = left.futures[packet_id] = asyncio.get_running_loop().create_future()
        await right.send_serialized(payload, flags, packet_id)
        if stream:
            received_flags, error, _ = await queue.get()
            assert received_flags == Flags.EXCEPTION | Flags.RESPONSE
            assert isinstance(error, PermissionError)
            left.streams.pop(packet_id)
        else:
            with pytest.raises(PermissionError):
                await future
        assert not marker.exists()
        assert sys.meta_path == meta_path
        assert set(sys.modules) == modules
        assert name not in left.known_modules
        assert left.fragments.total_bytes == 0
        assert await ping(left) == 42


@pytest.mark.asyncio
async def test_module_request_returns_permission_error():
    async with connected() as (left, right):
        with pytest.raises(PermissionError):
            await right._call(Ping, Flags.SYNC | Flags.REQUEST)
        assert await ping(left) == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [-1, 1], ids=["inline", "worker"])
@pytest.mark.parametrize("stream", [False, True], ids=["result", "stream"])
async def test_inline_tool_response_never_executes_source(tmp_path, threshold, stream):
    marker = tmp_path / "executed"
    definition = {
        "name": "Forbidden",
        "source": f"open({str(marker)!r}, 'w').close()\nclass Forbidden(Tool):\n    pass\n" + "# padding\n" * 30000,
    }

    class Inline:
        def __reduce__(self):
            return tool_from_dict, (definition,)

    payload = pickle.dumps(Inline())
    flags = Flags.RPC | Flags.RESPONSE
    if stream:
        payload = BaseProtocol.batch([payload])
        flags |= Flags.STREAM | Flags.BATCH
    async with connected(threshold=threshold) as (left, right):
        packet_id = left.get_id()
        if stream:
            queue: asyncio.Queue[tuple[Flags, Any, int]] = asyncio.Queue()
            left.streams[packet_id] = queue
        else:
            future = left.futures[packet_id] = asyncio.get_running_loop().create_future()
        await right.send_serialized(payload, flags, packet_id)
        if stream:
            received_flags, error, _ = await queue.get()
            assert received_flags == Flags.EXCEPTION | Flags.RESPONSE
            assert isinstance(error, PermissionError)
            left.streams.pop(packet_id)
        else:
            with pytest.raises(PermissionError):
                await future
        assert not marker.exists()
        assert left.fragments.total_bytes == 0
        assert await ping(left) == 42


@pytest.mark.parametrize(
    "definition",
    [
        {"name": "Ping", "source": ""},
        {"name": "Ping", "module": "not_imported"},
        {"name": "Missing", "module": __name__},
        {"name": "sys", "module": __name__},
        {"name": "Ping", "module": __name__, "sources": {"extra": {}}},
        {"name": "Ping", "module": __name__, "dependencies": {"extra": {}}},
    ],
    ids=["inline", "unloaded", "missing", "not-tool", "sources", "dependencies"],
)
def test_tool_references_cannot_bring_sources(definition):
    class Reference:
        def __reduce__(self):
            return tool_from_dict, (definition,)

    with pytest.raises(PermissionError):
        PacketUnpickler.loads(pickle.dumps(Reference()))


@pytest.mark.asyncio
@pytest.mark.parametrize("accept_code", [True], ids=["trusted-code-peer"])
async def test_previously_sent_tool_class_can_return(protocol):
    assert await protocol(Ping.echo, Ping) is Ping


def test_explicit_code_permission_keeps_inline_tool_loading():
    class Inline(Tool):
        @staticmethod
        def answer():
            return 42

    restored = PacketUnpickler.loads(pickle.dumps(Inline), accept_code=True)
    assert restored.answer() == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True], ids=["deny", "allow"])
async def test_command_factory_sets_only_local_permissions(transport_tls, enabled):
    async with await Protocol.from_command(
        python=sys.executable, tls=transport_tls, accept_code=enabled, accept_calls=enabled
    ) as remote:
        assert (remote.accept_code, remote.accept_calls) == (enabled, enabled)
        assert await remote(Permissions.inspect) == [(True, True)]


@pytest.mark.parametrize("enabled", [False, True], ids=["deny", "allow"])
def test_sync_factory_forwards_local_permissions(transport_tls, enabled):
    from rmote.sync import Connection

    with Connection.from_local(tls=transport_tls, accept_code=enabled, accept_calls=enabled) as connection:
        assert connection._protocol is not None
        assert (connection._protocol.accept_code, connection._protocol.accept_calls) == (enabled, enabled)
        assert connection(Ping.echo) == 42
