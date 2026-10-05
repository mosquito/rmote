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
async def test_timeout_reaps_child_and_preserves_partial_output():
    with pytest.raises(subprocess.TimeoutExpired) as error:
        await async_process(
            sys.executable,
            "-c",
            "import os,time,subprocess,sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(os.getpid(), flush=True); time.sleep(60)",
            capture_output=True,
            text=True,
            timeout=1,
        )
    assert isinstance(error.value.output, bytes)
    pid = int(error.value.output)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_cancellation_reaps_child(tmp_path):
    ready = tmp_path / "pid"
    task = asyncio.create_task(
        async_process(
            sys.executable,
            "-c",
            "import os,time,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
            str(ready),
        )
    )
    try:
        async with asyncio.timeout(5):
            while not ready.exists() or not ready.read_text():
                await asyncio.sleep(0.01)
        pid = int(ready.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_exec_commands_can_progress_concurrently(protocol, tmp_path):
    # Local subprocess transport shares the filesystem with this test. Wait for
    # the first child to start before sending the RPC that releases it.
    ready, release = tmp_path / "ready", tmp_path / "release"
    waiting = asyncio.create_task(
        protocol(
            Exec.command,
            sys.executable,
            "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); p=pathlib.Path(sys.argv[2]); "
            "\nwhile not p.exists(): time.sleep(.01)",
            str(ready),
            str(release),
        )
    )
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
            await protocol(Exec.command, "touch", str(release))
            assert (await waiting).returncode == 0
    finally:
        release.touch()  # Also release the child if the assertion times out.
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
