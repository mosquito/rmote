"""Idle lifetime depends on connected clients, not shell output."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rmote.cli import sshmux
from rmote.protocol import Protocol
from tests.support.synchronization import ObservedEvent


@pytest.mark.asyncio
async def test_idle_timeout_starts_only_after_the_last_client_leaves(tmp_path, monkeypatch):
    server = sshmux.MuxServer(MagicMock(spec=Protocol), str(tmp_path / "socket"), idle_timeout=15)
    server.activity = activity = ObservedEvent()
    client = asyncio.current_task()
    server.clients[client] = MagicMock()  # type: ignore[index]
    started = asyncio.Event()
    expire = asyncio.Event()

    async def wait_for(awaitable, timeout):
        assert timeout == 15
        waiter = asyncio.create_task(awaitable)
        started.set()
        try:
            await expire.wait()
            assert not waiter.done()
            raise TimeoutError
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    monkeypatch.setattr(sshmux, "asyncio", SimpleNamespace(**dict(vars(asyncio), wait_for=wait_for)))
    idle = asyncio.create_task(server.stop_when_idle())
    try:
        await activity.entered.wait()
        assert not started.is_set()
        assert not server.stopping.is_set()
        server.client_closed(client)  # type: ignore[arg-type]
        await started.wait()
        assert not server.stopping.is_set()
        expire.set()
        await idle
        assert server.stopping.is_set()
    finally:
        idle.cancel()
        await asyncio.gather(idle, return_exceptions=True)
