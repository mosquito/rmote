import asyncio
from typing import Any

import pytest

from rmote.protocol import Flags
from rmote.sync import Connection
from tests.sync.tools import Methods


@pytest.mark.parametrize("explicit_timeout", [False, True])
@pytest.mark.parametrize("preloaded", [False, True])
def test_new_rpc_after_idle_eof_rejects_before_dispatch(monkeypatch, explicit_timeout, preloaded):
    connection = Connection.from_local(rpc_timeout=5.0)
    try:
        if preloaded:
            assert connection(Methods.echo, "warm up") == "warm up"
        assert connection._protocol is not None and connection._process is not None
        protocol, process = connection._protocol, connection._process

        async def disconnect() -> Exception | None:
            process.terminate()
            await protocol.wait_closed()
            await process.wait()
            return protocol._close_error

        error = connection._runtime.run(disconnect, timeout=5.0)
        assert isinstance(error, asyncio.IncompleteReadError)

        async def unexpected_dispatch(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("A new RPC reached Tool dispatch after idle EOF")

        monkeypatch.setattr(protocol, "_call_tool", unexpected_dispatch)
        with pytest.raises(ConnectionError, match="transport") as caught:
            if explicit_timeout:
                connection.call_with_timeout(5.0, Methods.echo, "after EOF")
            else:
                connection(Methods.echo, "after EOF")
        assert caught.value.__cause__ is error
    finally:
        connection.close()
    assert not connection._runtime._thread.is_alive()
    assert connection._process is not None and connection._process.returncode is not None
    with pytest.raises(RuntimeError, match="closed"):
        connection(Methods.echo, "after close")


def test_rpc_waiting_during_eof_preserves_transport_error(monkeypatch, tmp_path):
    with Connection.from_local(rpc_timeout=5.0) as connection:
        assert connection._protocol is not None and connection._process is not None
        protocol, process = connection._protocol, connection._process
        original_send = protocol.send

        async def disconnect_after_request(data: Any, flags: Flags, packet_id: int) -> None:
            await original_send(data, flags, packet_id)
            if flags & Flags.RPC and flags & Flags.REQUEST:
                process.terminate()

        monkeypatch.setattr(protocol, "send", disconnect_after_request)
        with pytest.raises(asyncio.IncompleteReadError):
            connection(Methods.pause, str(tmp_path / "started"))
    assert not connection._runtime._thread.is_alive()
    assert process.returncode is not None
