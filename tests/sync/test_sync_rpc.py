import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from rmote.sync import Connection
from tests.support.synchronization import wait_until
from tests.sync.tools import Methods


def wait_started(marker: Path) -> None:
    wait_until(lambda: marker.exists() and marker.read_text() == "started")


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


def written(connection: Connection) -> list[int]:
    """Collect the size of every frame this side writes from now on."""
    assert connection._protocol is not None
    sizes: list[int] = []
    original = connection._protocol.writer.write

    def counted(data: Any) -> None:
        sizes.append(len(data))
        original(data)

    connection._protocol.writer.write = counted  # type: ignore[method-assign]
    return sizes


def test_a_call_can_refuse_compression():
    """The frame of an uncompressed call carries the bytes as they are."""
    payload = (b"rmote carries source and facts " * 2048).decode()
    with Connection.from_local() as connection:
        assert connection(Methods.echo, payload) == payload
        sizes = written(connection)
        assert connection(Methods.echo, payload) == payload
        compressed = max(sizes)
        sizes.clear()
        assert connection.uncompressed(Methods.echo, payload) == payload
        raw = max(sizes)

    # The dictionary of the connection already holds this text, so the
    # ordinary call costs a reference to it. The refusal costs every byte.
    assert compressed * 50 < raw
    assert raw > len(payload)


def test_an_uncompressed_streaming_method_is_refused_before_the_call(tmp_path):
    marker = tmp_path / "started"
    with Connection.from_local() as connection:
        with pytest.raises(TypeError, match="asynchronous Protocol"):
            connection.uncompressed(Methods.stream, str(marker))
        assert not marker.exists()
        assert connection(Methods.echo, "still usable") == "still usable"
        assert pending(connection) == 0


@pytest.mark.parametrize("with_timeout", [False, True])
def test_streaming_methods_rejected_before_remote_execution(tmp_path, with_timeout):
    marker = tmp_path / "started"
    with Connection.from_local() as connection:
        with pytest.raises(TypeError, match="asynchronous Protocol"):
            if with_timeout:
                connection.call_with_timeout(5.0, Methods.stream, str(marker))
            else:
                connection(Methods.stream, str(marker))
        assert not marker.exists()
        assert connection(Methods.echo, "still usable") == "still usable"
        assert pending(connection) == 0


def test_remote_and_invalid_tool_errors():
    with Connection.from_local() as connection:
        with pytest.raises(FileNotFoundError, match="remote file"):
            connection(Methods.fail)
        with pytest.raises(ValueError, match="Tool"):
            connection(lambda: None)
        assert connection(Methods.echo, "still usable") == "still usable"
        assert pending(connection) == 0


def test_timeout_keeps_channel_and_remote_operation(tmp_path, deadline):
    marker = tmp_path / "operation"
    timer = deadline("rmote.sync")
    with Connection.from_local(rpc_timeout=timer.seconds) as connection:
        connection.call_with_timeout(5.0, Methods.echo, "warm up")
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(connection, Methods.pause, str(marker))
            wait_started(marker)
            timer.expire()
            with pytest.raises(TimeoutError):
                call.result(5)
        assert pending(connection) == 0
        assert marker.read_text() == "started"
        # Disabling the default deadline affects only this invocation.
        assert connection.call_with_timeout(None, Methods.echo, "after timeout") == "after timeout"
        connection.call_with_timeout(None, Methods.resume)
        assert marker.read_text() == "finished"
        assert connection(Methods.echo, "after late response") == "after late response"
        assert pending(connection) == 0


def test_first_tool_upload_uses_rpc_deadline(monkeypatch, deadline):
    timer = deadline("rmote.sync")
    entered = threading.Event()
    with Connection.from_local() as connection:
        assert connection._protocol is not None
        original = connection._protocol._call

        async def delayed(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(connection._protocol, "_call", delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(connection.call_with_timeout, timer.seconds, Methods.echo, "not uploaded")
            assert entered.wait(5)
            timer.expire()
            with pytest.raises(TimeoutError):
                call.result(5)
        assert pending(connection) == 0
        monkeypatch.setattr(connection._protocol, "_call", original)
        assert connection(Methods.echo, "retry upload") == "retry upload"


def test_timeout_during_send_closes_channel(monkeypatch, deadline):
    timer = deadline("rmote.sync")
    entered = threading.Event()
    with Connection.from_local() as connection:
        connection(Methods.echo, "warm up")
        assert connection._protocol is not None

        async def blocked_drain() -> None:
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(connection._protocol.writer, "drain", blocked_drain)
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(connection.call_with_timeout, timer.seconds, Methods.echo, "cancel after write")
            assert entered.wait(5)
            timer.expire()
            with pytest.raises(TimeoutError):
                call.result(5)
        assert pending(connection) == 0
        with pytest.raises(ConnectionError):
            connection(Methods.echo, "damaged channel")


def test_keyboard_interrupt_waits_for_local_cleanup(tmp_path, monkeypatch):
    marker = tmp_path / "interrupted"
    with Connection.from_local() as connection:
        connection(Methods.echo, "warm up")
        original = connection._runtime.result
        interrupted = False

        def result(future, timeout=None):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                wait_started(marker)
                raise KeyboardInterrupt
            return original(future, timeout)

        monkeypatch.setattr(connection._runtime, "result", result)
        with pytest.raises(KeyboardInterrupt):
            connection(Methods.pause, str(marker))
        assert pending(connection) == 0
        assert connection(Methods.echo, "after interrupt") == "after interrupt"


def test_close_finishes_waiting_call_and_rejects_new_calls(tmp_path):
    marker = tmp_path / "waiting"
    connection = Connection.from_local()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(connection, Methods.pause, str(marker))
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
