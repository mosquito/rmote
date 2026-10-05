"""Subprocess helpers transferred on demand with the tools that use them."""

import asyncio
import locale
import logging
import math
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, cast

__tool_package__ = "rmote.process"

__all__ = ["process", "async_process"]


def process(
    *cmd_and_args: str,
    stdin: None | bytes | str = None,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    shell: bool = False,
    check: bool = False,
    cwd: None | str | Path = None,
) -> subprocess.CompletedProcess[Any]:
    """
    function for execute a subprocess on the remote side,
    must be safe, do not share stdout/stderr of the child process,
    because it's protocol pipes.

    Tools must use this function or async_process to execute subprocesses,
    to avoid conflicts with protocol communication.

    String stdin is encoded in binary mode. Text mode requires string stdin.
    """
    logging.debug("Executing subprocess: %r", cmd_and_args)

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": capture_output,
        "text": text,
        "env": env,
        "shell": shell,
        "check": check,
        "cwd": cwd,
    }

    if stdin is not None:
        if text and isinstance(stdin, bytes):
            raise TypeError("stdin must be str when text=True")
        if isinstance(stdin, str) and not text:
            stdin = stdin.encode()
        # Use input= (not stdin=) so subprocess uses PIPE internally;
        # remove stdin=DEVNULL to avoid the "stdin and input may not both be used" error.
        del kwargs["stdin"]
        kwargs["input"] = stdin

    if not capture_output:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    return subprocess.run(cmd_and_args, **kwargs)


async def async_process(
    *cmd_and_args: str,
    stdin: bytes | str | None = None,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    shell: bool = False,
    check: bool = False,
    cwd: str | Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run a child asynchronously without sharing the RPC stdout/stderr pipes.

    Arguments and results follow :func:`process`. Shell mode accepts one
    expression. Text mode uses the locale encoding and universal newlines;
    string stdin in binary mode uses UTF-8. Output is discarded unless captured.

    ``timeout`` limits communication after spawn, in seconds; None disables it.
    Timeout raises ``subprocess.TimeoutExpired`` with captured bytes. Cancellation
    and timeout kill the POSIX child process group and reap the direct child.
    This local cleanup does not change RPC cancellation semantics: cancelling
    a caller's RPC does not itself stop a remote operation.
    """
    if not cmd_and_args or (shell and len(cmd_and_args) != 1):
        raise ValueError("Supply a command, or one expression with shell=True")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be finite and positive, or None")
    if text and isinstance(stdin, bytes):
        raise TypeError("stdin must be str when text=True")
    encoding = locale.getpreferredencoding(False)
    data = stdin.encode(encoding if text else "utf-8") if isinstance(stdin, str) else stdin
    options: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
        "env": env,
        "cwd": cwd,
        "start_new_session": True,
    }
    logging.debug("Executing asynchronous subprocess: %r", cmd_and_args)
    spawn = asyncio.create_task(
        asyncio.create_subprocess_shell(cmd_and_args[0], **options)
        if shell
        else asyncio.create_subprocess_exec(*cmd_and_args, **options)
    )
    try:
        child = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        child = await spawn
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await child.communicate()
        raise
    communication = asyncio.create_task(child.communicate(data))
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except BaseException as exc:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = await communication
        if isinstance(exc, TimeoutError):
            assert timeout is not None
            raise subprocess.TimeoutExpired(cmd_and_args, timeout, output=stdout, stderr=stderr) from exc
        raise
    output: str | bytes | None = stdout
    errors: str | bytes | None = stderr
    if text:
        output = stdout.decode(encoding).replace("\r\n", "\n").replace("\r", "\n") if stdout is not None else None
        errors = stderr.decode(encoding).replace("\r\n", "\n").replace("\r", "\n") if stderr is not None else None
    result = subprocess.CompletedProcess(cmd_and_args, cast(int, child.returncode), output, errors)
    if check:
        result.check_returncode()
    return result
