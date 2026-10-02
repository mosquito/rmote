"""Synchronous connections backed by the existing asynchronous protocol."""

from __future__ import annotations

import asyncio
import logging
import math
import subprocess
import sys
import threading
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, ParamSpec, Self, TypeVar, overload

from rmote._runtime import _Runtime
from rmote.protocol import Protocol

__all__ = ["Connection"]

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class _Operation:
    task: asyncio.Task[Any] | None = None


def _check_timeout(value: float | None, name: str, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number" + (" or None" if optional else ""))


class Connection:
    """Own a subprocess, protocol, and private background event loop.

    Use a factory to connect. Factories finish the handshake before returning.
    Close the connection explicitly or use a context manager.
    """

    def __init__(self, *, _factory: bool = False) -> None:
        if not _factory:
            raise TypeError("Use Connection.from_local() or Connection.from_ssh()")
        self._runtime = _Runtime()
        self._process: asyncio.subprocess.Process | None = None
        self._protocol: Protocol | None = None
        self._spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._loop_thread: threading.Thread | None = None
        self._state = "CONNECTING"
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._entered = False
        self._rpc_timeout: float | None = None
        self._close_timeout = 5.0

    @classmethod
    def from_local(
        cls,
        *,
        python: str = sys.executable,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stderr: int = subprocess.DEVNULL,
        connect_timeout: float | None = 30.0,
        rpc_timeout: float | None = None,
        close_timeout: float = 5.0,
    ) -> Self:
        """Start a local Python interpreter and complete its protocol handshake.

        Arguments go directly to subprocess exec, without a shell. If stderr
        is PIPE, the connection reads and discards it until process exit.
        """
        return cls._connect(
            [python, "-qui"],
            cwd=cwd,
            env=env,
            stderr=stderr,
            connect_timeout=connect_timeout,
            rpc_timeout=rpc_timeout,
            close_timeout=close_timeout,
        )

    @classmethod
    def from_ssh(
        cls,
        host: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity: str | None = None,
        python: str = "python3",
        ssh_options: list[str] | None = None,
        stderr: int = subprocess.DEVNULL,
        connect_timeout: float | None = 30.0,
        rpc_timeout: float | None = None,
        close_timeout: float = 5.0,
    ) -> Self:
        """Start SSH and complete the remote Python protocol handshake."""
        cmd = ["ssh", "-T"]
        if user is not None:
            cmd += ["-l", user]
        if port is not None:
            cmd += ["-p", str(port)]
        if identity is not None:
            cmd += ["-i", identity]
        if ssh_options:
            cmd += ssh_options
        cmd += [host, python, "-qui"]
        return cls._connect(
            cmd,
            cwd=None,
            env=None,
            stderr=stderr,
            connect_timeout=connect_timeout,
            rpc_timeout=rpc_timeout,
            close_timeout=close_timeout,
        )

    @classmethod
    def _connect(
        cls,
        cmd: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        stderr: int,
        connect_timeout: float | None,
        rpc_timeout: float | None,
        close_timeout: float,
    ) -> Self:
        _check_timeout(connect_timeout, "connect_timeout")
        _check_timeout(rpc_timeout, "rpc_timeout")
        _check_timeout(close_timeout, "close_timeout", optional=False)
        connection = cls(_factory=True)
        connection._rpc_timeout = rpc_timeout
        connection._close_timeout = close_timeout
        try:
            connection._runtime.run(lambda: connection._open(cmd, cwd, env, stderr, connect_timeout))
        except BaseException:
            try:
                connection.close()
            except BaseException:
                logging.exception("Failed to clean up an unsuccessful connection")
            raise
        connection._state = "OPEN"
        return connection

    async def _open(
        self,
        cmd: list[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        stderr: int,
        timeout: float | None,
    ) -> None:
        self._loop_thread = threading.current_thread()
        async with asyncio.timeout(timeout):
            # Shield process creation so cleanup can recover a process created
            # while the caller interrupts or the connection deadline expires.
            self._spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr,
                )
            )
            self._process = await asyncio.shield(self._spawn_task)
            self._start_stderr_reader()
            self._protocol = await Protocol.from_subprocess(self._process)
            await self._protocol.__aenter__()

    def _start_stderr_reader(self) -> None:
        if self._stderr_task is None and self._process is not None and self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))

    @staticmethod
    async def _drain_stderr(reader: asyncio.StreamReader) -> None:
        while await reader.read(65536):
            pass

    async def _close_async(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._cleanup_task)

    async def _cleanup(self) -> None:
        if self._process is None and self._spawn_task is not None:
            try:
                self._process = await self._spawn_task
            except Exception:
                return
        process = self._process
        if process is None:
            return
        self._start_stderr_reader()
        try:
            # Protocol does not own this process. Connection owns escalation.
            async with asyncio.timeout(self._close_timeout):
                if self._protocol is not None:
                    await self._protocol.__aexit__(None, None, None)
                elif process.stdin is not None:
                    process.stdin.close()
                await process.wait()
        except TimeoutError:
            await self._terminate(process)
        finally:
            if process.returncode is None:
                await self._terminate(process)
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            async with asyncio.timeout(1.0):
                await process.wait()
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    def _check_caller(self) -> None:
        if threading.current_thread() is self._loop_thread:
            raise RuntimeError("Cannot use a synchronous connection from its event loop thread")

    @overload
    def __call__(self, tool: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs) -> R: ...

    @overload
    def __call__(self, tool: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R: ...

    def __call__(self, tool: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call a Tool method using the connection's default RPC deadline.

        Both synchronous and asynchronous Tool methods return their result.
        Every keyword argument belongs to the remote method.
        """
        return self.call_with_timeout(self._rpc_timeout, tool, *args, **kwargs)

    @overload
    def call_with_timeout(
        self,
        timeout: float | None,
        tool: Callable[P, Coroutine[Any, Any, R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R: ...

    @overload
    def call_with_timeout(
        self,
        timeout: float | None,
        tool: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R: ...

    def call_with_timeout(
        self,
        timeout: float | None,
        tool: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call a Tool with a deadline covering upload, send, and response.

        None disables this call's deadline. Timeout and KeyboardInterrupt stop
        local waiting. They do not stop the remote operation. A cancellation
        during packet transmission can make the connection unusable.
        """
        self._check_caller()
        _check_timeout(timeout, "timeout")
        operation = _Operation()
        with self._state_lock:
            if self._state != "OPEN":
                raise RuntimeError("Connection is closing or closed")
            future = self._runtime.submit(lambda: self._invoke(operation, timeout, tool, args, kwargs))
        try:
            return future.result()
        except KeyboardInterrupt:
            future.cancel()
            try:
                self._runtime.run(lambda: self._wait_operation(operation))
            except RuntimeError:
                # A concurrent close owns finalization after runtime shutdown starts.
                pass
            raise

    async def _invoke(
        self,
        operation: _Operation,
        timeout: float | None,
        tool: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        operation.task = asyncio.current_task()
        assert self._protocol is not None
        async with asyncio.timeout(timeout):
            return await self._protocol._call_tool(tool, *args, **kwargs)

    @staticmethod
    async def _wait_operation(operation: _Operation) -> None:
        if operation.task is not None:
            await asyncio.gather(operation.task, return_exceptions=True)

    def close(self) -> None:
        """Close the protocol, reap the process, and join the runtime thread.

        Concurrent calls wait for the same cleanup. Repeated calls are safe.
        Process shutdown uses close_timeout, then terminate and kill.
        Cancellation and executor shutdown require cooperative local code.
        """
        self._check_caller()
        with self._close_lock:
            with self._state_lock:
                if self._state == "CLOSED":
                    return
                self._state = "CLOSING"
            try:
                self._runtime.run(self._close_async)
            except BaseException:
                # An interrupted wait must not cancel process cleanup when the
                # runtime shuts down. The cleanup task is shared and shielded.
                self._runtime.run(self._close_async)
                raise
            finally:
                try:
                    self._runtime.close()
                finally:
                    with self._state_lock:
                        self._state = "CLOSED"

    def __enter__(self) -> Self:
        self._check_caller()
        with self._state_lock:
            if self._state != "OPEN":
                raise RuntimeError("Connection is not open")
            if self._entered:
                raise RuntimeError("Connection context is already active")
            self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException:
            if exc_value is None:
                raise
            logging.exception("Failed to close connection while handling an exception")
