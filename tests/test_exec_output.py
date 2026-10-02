"""Exec preserves output locally and over subprocess, SSH, and Docker transports."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio

from rmote.protocol import Protocol
from rmote.tools.exec import Exec

pytestmark = pytest.mark.timeout(60)


@pytest_asyncio.fixture(params=["direct", "subprocess", "ssh", pytest.param("docker", marks=pytest.mark.docker)])
async def execute(request: pytest.FixtureRequest) -> AsyncIterator[Callable[..., Any]]:
    if request.param == "direct":

        async def direct(method, *args, **kwargs):
            return method(*args, **kwargs)

        yield direct
        return

    command = [sys.executable, "-qui"]
    if request.param == "ssh":
        port, key, user = request.getfixturevalue("local_sshd")
        command = [
            "ssh",
            "-T",
            "-p",
            str(port),
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            f"{user}@127.0.0.1",
            sys.executable,
            "-qui",
        ]
    elif request.param == "docker":
        docker = request.getfixturevalue("docker")
        command = [
            docker,
            "run",
            "--rm",
            "-i",
            os.environ.get("RMOTE_TEST_DOCKER_IMAGE", request.getfixturevalue("docker_image")),
            "python3",
            "-qui",
        ]
    child = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        remote = await Protocol.from_subprocess(child)
        async with remote:
            yield remote
    finally:
        if child.returncode is None:
            child.terminate()
        await child.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        (Exec.command, ("sh", "-c", "cat; printf diagnostic >&2")),
        (Exec.shell, ("cat; printf diagnostic >&2",)),
    ],
)
@pytest.mark.parametrize(
    "stdin", [b"hello\x00\xffworld", "hello\n", b"x" * (256 * 1024)], ids=["binary", "text", "large"]
)
async def test_output_and_input(execute, method, args, stdin):
    result = await execute(method, *args, stdin=stdin, capture_output=True)
    assert result.returncode == 0
    assert result.stdout == (stdin.encode() if isinstance(stdin, str) else stdin)
    assert result.stderr == b"diagnostic"
    # A second RPC detects output that escaped onto the protocol channel.
    empty = await execute(Exec.command, "true", capture_output=True)
    assert empty.stdout == empty.stderr == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        (Exec.command, ("sh", "-c", "printf out; printf err >&2; exit 7")),
        (Exec.shell, ("printf out; printf err >&2; exit 7",)),
    ],
)
async def test_failure_output(execute, method, args):
    result = await execute(method, *args, check=False, capture_output=True)
    assert result.returncode == 7
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    with pytest.raises(subprocess.CalledProcessError) as error:
        await execute(method, *args, capture_output=True)
    assert error.value.returncode == 7
    assert error.value.output == error.value.stdout == b"out"
    assert error.value.stderr == b"err"
    assert (await execute(Exec.command, "true")).returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [Exec.command, Exec.shell])
@pytest.mark.parametrize("options", [{}, {"capture_output": False}], ids=["default", "explicit"])
async def test_discarded_output(execute, method, capfd, options):
    for code in [0, 7]:
        script = f"cat; printf diagnostic >&2; exit {code}"
        args = ("sh", "-c", script) if method is Exec.command else (script,)
        result = await execute(method, *args, stdin=b"x" * (256 * 1024), check=False, **options)
        assert result.returncode == code
        assert result.stdout is None
        assert result.stderr is None
        if code:
            with pytest.raises(subprocess.CalledProcessError) as error:
                await execute(method, *args, **options)
            assert error.value.returncode == code
            assert error.value.stdout is None
            assert error.value.stderr is None
    assert (await execute(Exec.command, "true")).returncode == 0
    assert capfd.readouterr().out == ""
