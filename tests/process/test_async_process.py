import asyncio
import os
import subprocess
import sys

import pytest

from rmote.process import async_process
from rmote.tools.exec import Exec


@pytest.mark.asyncio
async def test_text_input_output_and_failure():
    result = await async_process(
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
        stdin="Привет\r\nworld\r",
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == "Привет\nworld\n"
    with pytest.raises(subprocess.CalledProcessError) as error:
        await async_process(
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(4)",
            text=True,
            capture_output=True,
            check=True,
        )
    assert error.value.output == "out\n"
    assert error.value.stderr == "err\n"
    assert error.value.returncode == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        await async_process(sys.executable, timeout=timeout)


@pytest.mark.asyncio
async def test_invalid_arguments():
    with pytest.raises(ValueError):
        await async_process()
    with pytest.raises(ValueError):
        await async_process("true", "extra", shell=True)
    with pytest.raises(TypeError):
        await async_process("cat", stdin=b"bytes", text=True)


@pytest.mark.asyncio
async def test_timeout_reaps_child_and_preserves_partial_output(deadline, fifo):
    timer = deadline("rmote.process")
    task = asyncio.create_task(
        async_process(
            sys.executable,
            "-c",
            "import os,threading,subprocess,sys; "
            "subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()']); "
            "print(os.getpid(), flush=True); "
            "open(sys.argv[1], 'wb', buffering=0).write(b'x'); threading.Event().wait()",
            str(fifo.path),
            capture_output=True,
            text=True,
            timeout=timer.seconds,
        )
    )
    try:
        assert await asyncio.to_thread(fifo.receive) == b"x"
        assert await asyncio.to_thread(timer.entered.wait, 10)
        timer.expire()
        with pytest.raises(subprocess.TimeoutExpired) as error:
            await task
        assert isinstance(error.value.output, bytes)
        pid = int(error.value.output)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_reaps_child(fifo):
    task = asyncio.create_task(
        async_process(
            sys.executable,
            "-c",
            "import os,threading,sys; "
            "open(sys.argv[1], 'wb', buffering=0).write(str(os.getpid()).encode()); threading.Event().wait()",
            str(fifo.path),
        )
    )
    try:
        pid = int(await asyncio.to_thread(fifo.receive, 32))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_exec_commands_can_progress_concurrently(protocol, fifo_factory):
    ready, release = fifo_factory(), fifo_factory()
    waiting = asyncio.create_task(
        protocol(
            Exec.command,
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'wb', buffering=0).write(b'x'); "
            "assert open(sys.argv[2], 'rb', buffering=0).read(1) == b'x'",
            str(ready.path),
            str(release.path),
        )
    )
    try:
        assert await asyncio.to_thread(ready.receive) == b"x"
        await protocol(
            Exec.command,
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'wb', buffering=0).write(b'x')",
            str(release.path),
        )
        assert (await waiting).returncode == 0
    finally:
        release.send()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
