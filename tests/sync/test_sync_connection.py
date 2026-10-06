import asyncio
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from rmote.sync import Connection
from tests.support.tool_cases.ping import Ping
from tests.sync.tools import Environment


def call(connection: Connection, method: Callable[..., Any], *args: Any) -> Any:
    async def invoke() -> Any:
        assert connection._protocol is not None
        return await connection._protocol(method, *args)

    return connection._runtime.run(invoke)


def test_local_context_environment_and_close(tmp_path):
    env = dict(os.environ, RMOTE_SYNC_TEST="local")
    with Connection.from_local(cwd=str(tmp_path), env=env) as connection:
        process = connection._process
        assert process is not None
        assert call(connection, Environment.inspect) == (str(tmp_path.resolve()), "local")
        with pytest.raises(RuntimeError, match="already active"):
            connection.__enter__()
    assert process.returncode is not None
    connection.close()
    with pytest.raises(RuntimeError, match="not open"):
        connection.__enter__()


def test_direct_constructor_rejected():
    with pytest.raises(TypeError, match="from_local"):
        Connection()


def test_command_transport_environment_and_close(tmp_path):
    with Connection.from_command(
        "env", "RMOTE_SYNC_TEST=command", python=sys.executable, cwd=str(tmp_path)
    ) as connection:
        process = connection._process
        assert process is not None
        assert connection(Environment.inspect) == (str(tmp_path.resolve()), "command")
    assert process.returncode is not None


@pytest.mark.parametrize("field", ["connect_timeout", "rpc_timeout", "close_timeout"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_before_start(field, value, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Invalid deadline must not create a runtime")

    monkeypatch.setattr("rmote.sync._Runtime", fail)
    with pytest.raises(ValueError, match=field):
        Connection.from_local(**{field: value})


def test_startup_failure_leaves_no_thread():
    threads = set(threading.enumerate())
    with pytest.raises(FileNotFoundError):
        Connection.from_local(python="/does/not/exist/python")
    assert set(threading.enumerate()) == threads


def fake_interpreter(tmp_path: Path, script: str) -> str:
    path = tmp_path / "fake-python"
    path.write_text(f"#!{sys.executable}\n" + script)
    path.chmod(0o755)
    return str(path)


def record_processes(
    monkeypatch: pytest.MonkeyPatch, *, pause_stderr: bool = False
) -> list[asyncio.subprocess.Process]:
    processes: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def create(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        if pause_stderr:
            pipe = process._transport.get_pipe_transport(2)  # type: ignore[attr-defined]
            pipe.pause_reading()
            assert not pipe.is_reading()
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return processes


def test_eof_before_handshake_reaps_process(tmp_path, monkeypatch):
    executable = fake_interpreter(tmp_path, "raise SystemExit(0)\n")
    processes = record_processes(monkeypatch)
    threads = set(threading.enumerate())
    with pytest.raises((ConnectionError, BrokenPipeError)):
        Connection.from_local(python=executable)
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert set(threading.enumerate()) == threads


@pytest.mark.parametrize("pause_stderr", [False, True], ids=["reading", "paused"])
def test_handshake_timeout_closes_pipes_and_kills_stubborn_process(
    tmp_path, monkeypatch, deadline, fifo, pause_stderr
):
    timer = deadline("rmote.sync")
    executable = fake_interpreter(
        tmp_path,
        (
            "import os, signal, threading\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"with open({str(fifo.path)!r}, 'wb', buffering=0) as ready: ready.write(b'x')\n"
            "try: os.write(2, b'x' * 1048576)\n"
            "except BrokenPipeError: pass\n"
            "threading.Event().wait()\n"
        ),
    )
    processes = record_processes(monkeypatch, pause_stderr=pause_stderr)
    threads = set(threading.enumerate())
    with ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(
            Connection.from_local,
            python=executable,
            stderr=subprocess.PIPE,
            connect_timeout=timer.seconds,
            close_timeout=0.1,
        )
        assert fifo.receive() == b"x"
        timer.expire()
        with pytest.raises(TimeoutError):
            start.result(5)
    assert processes[0].returncode == -signal.SIGKILL
    transport = processes[0]._transport  # type: ignore[attr-defined]
    assert transport.is_closing()
    assert all(transport.get_pipe_transport(fd).is_closing() for fd in (0, 1, 2))
    assert set(threading.enumerate()) == threads


def test_stderr_pipe_during_successful_handshake(tmp_path):
    executable = fake_interpreter(
        tmp_path,
        (f"import os, sys\nos.write(2, b'x' * 1048576)\nos.execv({sys.executable!r}, [{sys.executable!r}, '-qui'])\n"),
    )
    with Connection.from_local(python=executable, stderr=subprocess.PIPE, connect_timeout=5.0) as connection:
        assert call(connection, Environment.inspect)[0] == os.getcwd()
    assert connection._process is not None
    assert connection._process.returncode is not None


def test_ssh_arguments_and_real_protocol(monkeypatch):
    original = asyncio.create_subprocess_exec
    commands = []

    async def create(*args, **kwargs):
        commands.append(args)
        return await original(sys.executable, "-qui", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    options = ["-o", "BatchMode=yes"]
    with Connection.from_ssh(
        "embedded@host",
        user="override",
        port=2222,
        identity="key with spaces",
        python="remote-python",
        ssh_options=options,
    ) as connection:
        assert call(connection, Environment.inspect)[0] == os.getcwd()
    assert commands == [
        (
            "ssh",
            "-T",
            "-l",
            "override",
            "-p",
            "2222",
            "-i",
            "key with spaces",
            "-o",
            "BatchMode=yes",
            "embedded@host",
            "remote-python",
            "-qui",
        )
    ]
    assert options == ["-o", "BatchMode=yes"]


def test_concurrent_and_loop_thread_close():
    connection = Connection.from_local()

    async def from_loop():
        with pytest.raises(RuntimeError, match="event loop thread"):
            connection.close()

    connection._runtime.run(from_loop)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(connection.close) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)
    assert connection._process is not None
    assert connection._process.returncode is not None


def test_body_exception_survives_cleanup_error(monkeypatch):
    connection = Connection.from_local()
    connection.close()

    def fail():
        raise OSError("cleanup failed")

    monkeypatch.setattr(connection, "close", fail)
    error = ValueError("body failed")
    # Use __exit__ directly because entering a closed connection is rejected.
    connection.__exit__(type(error), error, None)
    with pytest.raises(OSError, match="cleanup failed"):
        connection.__exit__(None, None, None)


def test_timeout_during_process_creation_recovers_process(monkeypatch, deadline):
    timer = deadline("rmote.sync")
    original = asyncio.create_subprocess_exec
    original_close = Connection._close_async
    processes = []
    spawned = threading.Event()
    release = asyncio.Event()

    async def slow_create(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    async def close(self):
        release.set()
        await original_close(self)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_create)
    monkeypatch.setattr(Connection, "_close_async", close)
    threads = set(threading.enumerate())
    with ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(Connection.from_local, connect_timeout=timer.seconds, close_timeout=0.1)
        assert spawned.wait(5)
        timer.expire()
        with pytest.raises(TimeoutError):
            start.result(5)
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert set(threading.enumerate()) == threads


def test_keyboard_interrupt_during_handshake_recovers_process(tmp_path, monkeypatch):
    from rmote._runtime import _Runtime

    executable = fake_interpreter(tmp_path, "import threading\nthreading.Event().wait()\n")
    original_run = _Runtime.run
    spawned = threading.Event()
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    interrupted = False

    async def create(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process

    def interrupt_once(self, factory, *, timeout=None):
        nonlocal interrupted
        if interrupted:
            return original_run(self, factory, timeout=timeout)
        interrupted = True
        future = self.submit(factory)
        assert spawned.wait(5)
        future.cancel()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(_Runtime, "run", interrupt_once)
    threads = set(threading.enumerate())
    with pytest.raises(KeyboardInterrupt):
        Connection.from_local(python=executable, close_timeout=0.1)
    assert processes[0].returncode is not None
    assert set(threading.enumerate()) == threads


def test_a_call_that_reaches_the_loop_after_a_close_is_refused() -> None:
    """The state is read without a lock, so the loop thread checks it again."""
    with Connection.from_local(python=sys.executable) as connection:
        assert connection(Ping.ping) == 1
        connection._state = "CLOSING"
        try:
            with pytest.raises(RuntimeError, match="closing or closed"):
                connection._runtime.run(lambda: connection._invoke(None, Ping.ping, (), {}))
        finally:
            connection._state = "OPEN"
