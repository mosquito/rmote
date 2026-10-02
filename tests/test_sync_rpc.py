import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from rmote.sync import Connection
from tests.sync_tools import Methods


def wait_started(marker: Path) -> None:
    deadline = time.monotonic() + 5
    while not marker.exists():
        if time.monotonic() >= deadline:
            pytest.fail("Remote method did not start")
        time.sleep(0.01)


def pending(connection: Connection) -> int:
    async def inspect() -> int:
        assert connection._protocol is not None
        return len(connection._protocol.futures)

    return connection._runtime.run(inspect)


@pytest.mark.parametrize("method", [Methods.echo, Methods.async_echo, Methods.class_echo, Methods.async_class_echo])
def test_sync_and_async_methods(method):
    with Connection.from_local() as connection:
        assert connection(method, "hello") == "hello"
        assert connection.call_with_timeout(5.0, method, "again") == "again"
        assert pending(connection) == 0


def test_remote_keyword_arguments():
    with Connection.from_local(rpc_timeout=5.0) as connection:
        assert connection(Methods.keywords, timeout=7.0, tool="remote") == (7.0, "remote")
        assert connection.call_with_timeout(3.0, Methods.keywords, timeout=8.0, tool="other") == (8.0, "other")


def test_remote_and_invalid_tool_errors():
    with Connection.from_local() as connection:
        with pytest.raises(FileNotFoundError, match="remote file"):
            connection(Methods.fail)
        with pytest.raises(ValueError, match="Tool"):
            connection(lambda: None)
        assert connection(Methods.echo, "still usable") == "still usable"
        assert pending(connection) == 0


def test_timeout_keeps_channel_and_remote_operation(tmp_path):
    marker = tmp_path / "operation"
    with Connection.from_local(rpc_timeout=0.05) as connection:
        connection.call_with_timeout(5.0, Methods.echo, "warm up")
        with pytest.raises(TimeoutError):
            connection(Methods.pause, 0.2, str(marker))
        assert pending(connection) == 0
        assert marker.read_text() == "started"
        # Disabling the default deadline affects only this invocation.
        assert connection.call_with_timeout(None, Methods.echo, "after timeout") == "after timeout"
        deadline = time.monotonic() + 5
        while marker.read_text() != "finished":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert connection(Methods.echo, "after late response") == "after late response"
        assert pending(connection) == 0


def test_first_tool_upload_uses_rpc_deadline(monkeypatch):
    with Connection.from_local() as connection:
        assert connection._protocol is not None
        original = connection._protocol._call

        async def delayed(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return await original(*args, **kwargs)

        monkeypatch.setattr(connection._protocol, "_call", delayed)
        with pytest.raises(TimeoutError):
            connection.call_with_timeout(0.01, Methods.echo, "not uploaded")
        assert pending(connection) == 0
        monkeypatch.setattr(connection._protocol, "_call", original)
        assert connection(Methods.echo, "retry upload") == "retry upload"


def test_timeout_during_send_closes_channel(monkeypatch):
    with Connection.from_local() as connection:
        connection(Methods.echo, "warm up")
        assert connection._protocol is not None

        async def blocked_drain() -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(connection._protocol.writer, "drain", blocked_drain)
        with pytest.raises(TimeoutError):
            connection.call_with_timeout(0.02, Methods.echo, "cancel after write")
        assert pending(connection) == 0
        with pytest.raises(ConnectionError):
            connection(Methods.echo, "damaged channel")


def test_keyboard_interrupt_waits_for_local_cleanup(tmp_path, monkeypatch):
    marker = tmp_path / "interrupted"
    with Connection.from_local() as connection:
        connection(Methods.echo, "warm up")
        original = connection._runtime.submit
        interrupted = False

        def submit(factory):
            nonlocal interrupted
            future = original(factory)
            if not interrupted:
                interrupted = True

                def interrupt(timeout=None):
                    wait_started(marker)
                    raise KeyboardInterrupt

                monkeypatch.setattr(future, "result", interrupt)
            return future

        monkeypatch.setattr(connection._runtime, "submit", submit)
        with pytest.raises(KeyboardInterrupt):
            connection(Methods.pause, 0.2, str(marker))
        assert pending(connection) == 0
        assert connection(Methods.echo, "after interrupt") == "after interrupt"


def test_close_finishes_waiting_call_and_rejects_new_calls(tmp_path):
    marker = tmp_path / "waiting"
    connection = Connection.from_local()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(connection, Methods.pause, 60.0, str(marker))
            wait_started(marker)
            connection.close()
            with pytest.raises(ConnectionError):
                future.result(timeout=5)
        with pytest.raises(RuntimeError, match="closed"):
            connection(Methods.echo, "closed")
    finally:
        connection.close()


def test_eof_preserves_transport_error():
    with Connection.from_local() as connection:
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            connection(Methods.exit)
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            connection(Methods.echo, "after EOF")


def test_loop_thread_call_rejected():
    with Connection.from_local() as connection:

        async def from_loop() -> None:
            with pytest.raises(RuntimeError, match="event loop thread"):
                connection(Methods.echo, "loop")

        connection._runtime.run(from_loop)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_per_call_timeout(value):
    with Connection.from_local() as connection:
        with pytest.raises(ValueError, match="timeout"):
            connection.call_with_timeout(value, Methods.echo, "invalid")
        assert connection(Methods.echo, "still usable") == "still usable"
