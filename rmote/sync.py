"""Synchronous connections backed by the existing asynchronous protocol."""

from __future__ import annotations

import asyncio
import logging
import math
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from types import TracebackType
from typing import Any, Never, ParamSpec, Self, TypeVar, overload

from rmote._runtime import _Runtime
from rmote.protocol import Protocol, streaming_method

__all__ = ["Connection"]

P = ParamSpec("P")
R = TypeVar("R")


def _check_timeout(value: float | None, name: str, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number" + (" or None" if optional else ""))


class Connection:
    """Own a subprocess, protocol, and private background event loop.

    Use a factory to connect. Factories finish the handshake before returning.
    Close the connection explicitly or use a context manager.

    Multiple caller threads can share the connection. Remote log records invoke
    local logging handlers in a delivery thread of their own, so a slow handler
    does not hold up the calls. Those handlers must not call synchronous methods
    of this connection, because closing waits for the queued records.

    Throughput rises with the number of caller threads and then flattens,
    because the Python work of a call is serialized by the interpreter lock:
    about 28 us in the loop thread and 23 us in the caller. Measured on a local
    transport with a tool that returns at once, sixteen threads reach 78 percent
    of what one connection can do, thirty-two reach 88 percent and sixty-four
    reach 96 percent; further threads add less than a percent each. Nothing
    collapses beyond that point, so a wider pool only stops paying off. Open
    another connection to go faster.
    """

    def __init__(self, *, _factory: bool = False) -> None:
        if not _factory:
            raise TypeError("Use Connection.from_local(), Connection.from_command() or Connection.from_ssh()")
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
    def from_command(
        cls,
        *argv: str,
        python: str = "python3",
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stderr: int = subprocess.PIPE,
        start_new_session: bool = False,
        connect_timeout: float | None = 30.0,
        rpc_timeout: float | None = None,
        close_timeout: float = 5.0,
    ) -> Self:
        """Connect through a command that passes stdin and stdout unchanged.

        Append ``python -qui`` to *argv*, without a shell. For example,
        ``from_command("docker", "exec", "-i", "app")`` starts
        ``docker exec -i app python3 -qui``. Do not request a transport TTY.
        Empty *argv* starts the specified interpreter locally.

        ``cwd`` and ``env`` configure the local transport process. Stderr and
        deadlines behave as in :meth:`from_local` and :meth:`from_ssh`.
        ``start_new_session`` isolates the transport from terminal signals,
        useful when the caller handles Ctrl-C itself.
        """
        return cls._connect(
            [*argv, python, "-qui"],
            cwd=cwd,
            env=env,
            stderr=stderr,
            connect_timeout=connect_timeout,
            rpc_timeout=rpc_timeout,
            close_timeout=close_timeout,
            start_new_session=start_new_session,
        )

    @classmethod
    def from_local(
        cls,
        *,
        python: str = sys.executable,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stderr: int = subprocess.PIPE,
        connect_timeout: float | None = 30.0,
        rpc_timeout: float | None = None,
        close_timeout: float = 5.0,
    ) -> Self:
        """Start a local Python interpreter and complete its protocol handshake.

        Arguments go directly to subprocess exec, without a shell. The default
        stderr is a pipe that the connection reads: its last lines explain a
        failed start, and the rest is dropped.
        """
        return cls.from_command(
            python=python,
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
        stderr: int = subprocess.PIPE,
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
        cmd.append(host)
        return cls.from_command(
            *cmd,
            python=python,
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
        start_new_session: bool = False,
    ) -> Self:
        _check_timeout(connect_timeout, "connect_timeout")
        _check_timeout(rpc_timeout, "rpc_timeout")
        _check_timeout(close_timeout, "close_timeout", optional=False)
        connection = cls(_factory=True)
        connection._rpc_timeout = rpc_timeout
        connection._close_timeout = close_timeout
        try:
            connection._runtime.run(
                lambda: connection._open(cmd, cwd, env, stderr, connect_timeout, start_new_session)
            )
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
        start_new_session: bool = False,
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
                    start_new_session=start_new_session,
                )
            )
            self._process = await asyncio.shield(self._spawn_task)
            # The protocol reads the transport stderr itself, and it keeps the
            # lines of the start for the failure of the handshake.
            self._protocol = await Protocol.from_subprocess(self._process)
            await self._protocol.__aenter__()

    def _start_stderr_reader(self) -> None:
        """Drain the transport stderr while no protocol reads it.

        A protocol reads that stream itself and keeps the lines of the start,
        so a second reader would take them from it. This one covers the case
        where the connection never reached a protocol: the pipe still has to
        be emptied, or the process blocks on its own write.
        """
        if self._protocol is not None or self._stderr_task is not None:
            return
        if self._process is not None and self._process.stderr is not None:
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
        # A log handler runs in the delivery thread, which the close waits for.
        # A call from there would wait for itself.
        protocol = self._protocol
        if protocol is not None and threading.current_thread() is protocol._logs.worker:
            raise RuntimeError("Cannot use a synchronous connection from its log delivery thread")

    @overload
    def __call__(self, tool: Callable[P, AsyncIterator[Any]], /, *args: P.args, **kwargs: P.kwargs) -> Never: ...

    @overload
    def __call__(self, tool: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs) -> R: ...

    @overload
    def __call__(self, tool: Callable[P, R | Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs) -> R: ...

    def __call__(self, tool: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call a Tool method using the connection's default RPC deadline.

        Both synchronous and asynchronous Tool methods return their result.
        Every keyword argument belongs to the remote method.
        Async generator methods require the asynchronous Protocol client and
        raise TypeError here before any remote call.
        """
        return self.call_with_timeout(self._rpc_timeout, tool, *args, **kwargs)

    @overload
    def call_with_timeout(
        self,
        timeout: float | None,
        tool: Callable[P, AsyncIterator[Any]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Never: ...

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
        tool: Callable[P, R | Coroutine[Any, Any, R]],
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
        After a detected transport failure, new calls raise ConnectionError
        with the original error as their cause. Calls already waiting for a
        response propagate the original transport error.
        Async generator methods raise TypeError; use Protocol for streaming.
        """
        return self._dispatch(timeout, tool, args, kwargs, True)

    @overload
    def uncompressed(self, tool: Callable[P, AsyncIterator[Any]], /, *args: P.args, **kwargs: P.kwargs) -> Never: ...

    @overload
    def uncompressed(self, tool: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs) -> R: ...

    @overload
    def uncompressed(
        self, tool: Callable[P, R | Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> R: ...

    def uncompressed(self, tool: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call without compressing the request or the response.

        Use it for data that cannot shrink, such as an archive or an image:
        the bytes then skip the dictionary of the connection instead of paying
        a deflate pass that gains nothing.

        The deadline is the connection's default. All arguments belong to the
        tool, including a keyword named compressed, and a neighbouring call
        keeps its own policy. Async generator methods raise TypeError, as they
        do for an ordinary call.
        """
        return self._dispatch(self._rpc_timeout, tool, args, kwargs, False)

    def _dispatch(
        self,
        timeout: float | None,
        tool: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        compressed: bool,
    ) -> Any:
        """Check the caller and the state, then run one call on the loop."""
        self._check_caller()
        _check_timeout(timeout, "timeout")
        if streaming_method(tool):
            raise TypeError("Streaming Tool methods require the asynchronous Protocol client")
        # The state is read without the lock, and the loop thread checks it
        # again before the call leaves. A closing connection is therefore
        # refused, and an open one does not pay for a second lock per call.
        if self._state != "OPEN":
            raise RuntimeError("Connection is closing or closed")
        future = self._runtime.submit(lambda: self._invoke(timeout, tool, args, kwargs, compressed))
        try:
            return self._runtime.result(future)
        except KeyboardInterrupt:
            future.cancel()
            try:
                self._runtime.wait(future)
            except RuntimeError:
                # A concurrent close owns finalization after runtime shutdown starts.
                pass
            raise

    async def _invoke(
        self,
        timeout: float | None,
        tool: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        compressed: bool = True,
    ) -> Any:
        if self._state != "OPEN":
            raise RuntimeError("Connection is closing or closed")
        assert self._protocol is not None
        if self._protocol._closed.is_set():
            raise ConnectionError("Connection transport is closed") from self._protocol._close_error
        # Every call runs as its own task, so the policy of this one stays in
        # its own context and no neighbour sees it.
        token = self._protocol._compression.set(compressed)
        try:
            if timeout is None:
                return await self._protocol._call_tool(tool, *args, **kwargs)
            async with asyncio.timeout(timeout):
                return await self._protocol._call_tool(tool, *args, **kwargs)
        finally:
            self._protocol._compression.reset(token)

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
