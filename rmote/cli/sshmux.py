"""OpenSSH control socket backed by Vty sessions on one rmote connection.

Implements passenger sessions from OpenSSH's PROTOCOL.mux, version 4. The
control connection carries requests and exit status; SCM_RIGHTS supplies the
local stdio descriptors. Only their contents travel over rmote.
"""

import argparse
import array
import asyncio
import contextlib
import errno
import fcntl
import logging
import math
import os
import select
import signal
import socket
import stat
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO

from rmote.cli.sftp import SftpServer
from rmote.cli.shell import EscapeFilter, NonBlocking, regular_file, terminal_size, wait_readable, write_all
from rmote.protocol import Protocol
from rmote.tools.agent import Agent
from rmote.tools.vty import Vty, launcher_source

log = logging.getLogger(__name__)
UINT32 = struct.Struct(">I")
MAX_PACKET = 256 * 1024
HANDSHAKE_TIMEOUT = 30
START_TIMEOUT = 30


class Message(IntEnum):
    """Wire identifiers from OpenSSH's multiplexing protocol."""

    HELLO = 0x00000001
    NEW_SESSION = 0x10000002
    ALIVE_CHECK = 0x10000004
    TERMINATE = 0x10000005
    OK = 0x80000001
    FAILURE = 0x80000003
    EXIT_MESSAGE = 0x80000004
    ALIVE = 0x80000005
    SESSION_OPENED = 0x80000006


def packet(*values: int | str) -> bytes:
    """Encode a length-prefixed mux packet of uint32 values and strings."""
    parts = []
    for value in values:
        if isinstance(value, str):
            data = value.encode("utf-8", "surrogateescape")
            parts.append(UINT32.pack(len(data)) + data)
        else:
            parts.append(UINT32.pack(value))
    data = b"".join(parts)
    return UINT32.pack(len(data)) + data


@dataclass
class Packet:
    """Bounded reader for one mux payload."""

    data: bytes
    offset: int = 0

    def take(self, size: int) -> bytes:
        if size > len(self.data) - self.offset:
            raise ValueError("Truncated mux packet")
        start = self.offset
        self.offset += size
        return self.data[start : self.offset]

    def uint32(self) -> int:
        return int(UINT32.unpack(self.take(4))[0])

    def string(self) -> str:
        data = self.take(self.uint32())
        if b"\0" in data:
            raise ValueError("NUL in mux string")
        return data.decode("utf-8", "surrogateescape")

    def end(self) -> None:
        if self.offset != len(self.data):
            raise ValueError("Unexpected data in mux packet")


@dataclass
class SessionRequest:
    """The session options sent by an unmodified ssh client."""

    tty: bool
    x11: bool
    agent: bool
    subsystem: bool
    escape: int
    term: str
    command: str
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, message: Packet) -> "SessionRequest":
        message.string()  # Reserved field.
        # PROTOCOL.mux calls these booleans, but OpenSSH encodes them as uint32.
        result = cls(
            bool(message.uint32()),
            bool(message.uint32()),
            bool(message.uint32()),
            bool(message.uint32()),
            message.uint32(),
            message.string(),
            message.string(),
        )
        if result.escape != 0xFFFFFFFF and result.escape > 255:
            raise ValueError("Invalid escape character")
        while message.offset < len(message.data):
            name, separator, value = message.string().partition("=")
            if not separator or not name:
                raise ValueError("Invalid environment variable")
            result.env[name] = value
        return result


async def read_fd(fd: int, size: int) -> bytes:
    """Read a passed descriptor without blocking the shared event loop."""
    while True:
        if not regular_file(fd):
            try:
                await wait_readable(fd)
            except OSError:
                # Linux selectors cannot register /dev/null; reading it is EOF.
                if os.fstat(fd).st_rdev != os.stat(os.devnull).st_rdev:
                    raise
        try:
            return os.read(fd, size)
        except BlockingIOError:
            continue


class MuxClient:
    """Own one control connection and, when requested, one Vty session."""

    def __init__(self, server: "MuxServer", connection: socket.socket) -> None:
        self.server = server
        self.connection = connection
        self.loop = asyncio.get_running_loop()
        self.fds: list[int] = []
        self.flags: list[NonBlocking] = []
        self.key: int | None = None
        self.resize = asyncio.Event()
        self.request: SessionRequest | None = None

    async def receive(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = await self.loop.sock_recv(self.connection, size - len(chunks))
            if not chunk:
                raise EOFError("Mux client disconnected")
            chunks.extend(chunk)
        return bytes(chunks)

    async def receive_packet(self) -> Packet:
        length = int(UINT32.unpack(await self.receive(4))[0])
        if not 4 <= length <= MAX_PACKET:
            raise ValueError("Invalid mux packet length")
        return Packet(await self.receive(length))

    async def send(self, *values: int | str) -> None:
        await self.loop.sock_sendall(self.connection, packet(*values))

    async def receive_fd(self) -> None:
        """Receive exactly one descriptor, closing malformed ancillary data."""
        while True:
            try:
                data, ancillary, flags, _ = self.connection.recvmsg(
                    1, socket.CMSG_SPACE(16 * array.array("i").itemsize)
                )
                break
            except BlockingIOError:
                await wait_readable(self.connection.fileno())
        received: list[int] = []
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                integers = array.array("i")
                integers.frombytes(value[: len(value) - len(value) % integers.itemsize])
                received.extend(integers)
        try:
            if data != b"\0" or flags & socket.MSG_CTRUNC or len(received) != 1:
                raise ValueError("Expected one stdio descriptor")
            os.set_inheritable(received[0], False)
            self.fds.append(received.pop())
        finally:
            for fd in received:
                os.close(fd)

    async def run(self) -> None:
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                await self.send(Message.HELLO, 4)
                hello = await self.receive_packet()
                if hello.uint32() != Message.HELLO or hello.uint32() != 4:
                    raise ValueError("Expected OpenSSH mux protocol version 4")
                while hello.offset < len(hello.data):
                    hello.string()
                    hello.string()
            while True:
                async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                    message = await self.receive_packet()
                    kind, request_id = message.uint32(), message.uint32()
                    if kind == Message.ALIVE_CHECK:
                        message.end()
                        await self.send(Message.ALIVE, request_id, os.getpid())
                        continue
                    if kind == Message.TERMINATE:
                        message.end()
                        await self.send(Message.OK, request_id)
                        self.server.stopping.set()
                        return
                    if kind != Message.NEW_SESSION:
                        await self.send(
                            Message.FAILURE, request_id, "rmote sshmux supports shell and exec sessions only"
                        )
                        return
                    self.request = SessionRequest.read(message)
                    for _ in range(3):
                        await self.receive_fd()
                await self.session(request_id)
                return
        except (EOFError, ConnectionError):
            pass
        except (OSError, ValueError, TimeoutError) as error:
            log.debug("Mux client closed: %s", error)
        except Exception:
            log.exception("Mux client failed")
        finally:
            # Flags are shared with the client's descriptors. Restore in reverse
            # order because stdin/stdout/stderr may share an open file description.
            for item in reversed(self.flags):
                item.restore()
            for fd in self.fds:
                os.close(fd)
            self.connection.close()

    async def session(self, request_id: int) -> None:
        request = self.request
        assert request is not None
        if request.subsystem:
            if request.command != "sftp":
                await self.send(Message.FAILURE, request_id, "Unknown subsystem")
                return
            if request.tty:
                await self.send(Message.FAILURE, request_id, "SFTP does not support a terminal")
                return
            await self.sftp(request_id)
            return
        for fd in self.fds:
            item = NonBlocking(fd)
            item.enter()
            self.flags.append(item)
        # A non-forwarded session must not inherit the bootstrap process's
        # agent, including for local and nested-SSH transports. Client SetEnv
        # cannot override the session's forwarding decision.
        env = dict(request.env, SSH_AUTH_SOCK="", SSH_AGENT_PID="")
        async with contextlib.AsyncExitStack() as resources:
            if request.agent:
                path = self.server.agent_path
                try:
                    if not path:
                        raise OSError("SSH_AUTH_SOCK is not set for the mux server")
                    env["SSH_AUTH_SOCK"] = await resources.enter_async_context(
                        Agent.forward(listener=self.server.protocol, path=path)
                    )
                except (OSError, ValueError, TimeoutError) as error:
                    warning = f"rmote sshmux: agent forwarding unavailable ({error}); continuing without forwarding.\n"
                    await write_all(self.fds[2], warning.encode())
            if request.x11:
                await write_all(
                    self.fds[2],
                    b"rmote sshmux: X11 forwarding is not supported; continuing without X11 forwarding.\n",
                )
            await self.shell_session(request_id, env)

    async def shell_session(self, request_id: int, env: dict[str, str]) -> None:
        request = self.request
        assert request is not None
        rows, cols = terminal_size(self.fds[0], self.fds[1])
        opening = asyncio.create_task(
            self.server.protocol(
                Vty.shell,
                request.command,
                rows=rows,
                cols=cols,
                term=request.term,
                env=env,
                want_pty=request.tty,
                launcher=self.server.motd_launcher if request.tty and not request.command else self.server.launcher,
            )
        )
        try:
            self.key = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # An open RPC may have created its child before cancellation arrives.
            # Collect its key and release it rather than abandoning the response.
            try:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(8):
                        key = await opening
                        await self.server.protocol(Vty.close, key)
            finally:
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            raise
        except Exception as error:
            await self.send(Message.FAILURE, request_id, str(error))
            return
        try:
            await self.send(Message.SESSION_OPENED, request_id, self.key)
            status = await self.bridge()
            await self.send(Message.EXIT_MESSAGE, self.key, status if status >= 0 else 128 - status)
        finally:
            # The control socket is the session lease. EOF, ~. and server shutdown
            # release this Vty without disturbing any of the other clients.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(8):
                    await self.server.protocol(Vty.close, self.key)
            self.key = None

    async def sftp(self, request_id: int) -> None:
        for fd in self.fds:
            item = NonBlocking(fd)
            item.enter()
            self.flags.append(item)

        async def read(size: int) -> bytes:
            return await read_fd(self.fds[0], size)

        async def write(data: bytes) -> None:
            await write_all(self.fds[1], data)

        # Session ids are local to a control connection. SFTP uses no Vty key.
        await self.send(Message.SESSION_OPENED, request_id, 0)
        session = asyncio.create_task(SftpServer(self.server.protocol, read, write).run())
        disconnected = asyncio.create_task(self.loop.sock_recv(self.connection, 1))
        try:
            done, _ = await asyncio.wait({session, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if session in done:
                try:
                    status = session.result()
                except (ValueError, OSError) as error:
                    await write_all(self.fds[2], f"rmote sshmux: {error}\n".encode())
                    status = 1
                await self.send(Message.EXIT_MESSAGE, 0, status)
        finally:
            session.cancel()
            disconnected.cancel()
            await asyncio.gather(session, disconnected, return_exceptions=True)

    async def input(self) -> bool:
        assert self.key is not None
        request = self.request
        assert request is not None
        escape = bytes((request.escape,)) if request.tty and request.escape != 0xFFFFFFFF else b""
        filtering = EscapeFilter(escape)
        while data := await read_fd(self.fds[0], Vty.CHUNK_SIZE):
            forward, closing = filtering.feed(data)
            if forward:
                await self.server.protocol(Vty.write, self.key, forward)
            if closing:
                return True
        if filtering.pending:
            await self.server.protocol(Vty.write, self.key, escape)
        await self.server.protocol(Vty.end_input, self.key)
        return False

    async def output(self, stderr: bool = False) -> None:
        assert self.key is not None
        method = Vty.stderr if stderr else Vty.output
        async for data in self.server.protocol(method, self.key):
            await write_all(self.fds[2 if stderr else 1], data)

    async def result(self) -> int:
        assert self.key is not None
        async with asyncio.TaskGroup() as group:
            group.create_task(self.output())
            group.create_task(self.output(stderr=True))
        return await self.server.protocol(Vty.wait, self.key)

    async def watch_resize(self) -> None:
        assert self.key is not None
        while True:
            await self.resize.wait()
            self.resize.clear()
            rows, cols = terminal_size(self.fds[0], self.fds[1])
            await self.server.protocol(Vty.resize, self.key, rows, cols)

    async def bridge(self) -> int:
        output = asyncio.create_task(self.result())
        input_task = asyncio.create_task(self.input())
        disconnected = asyncio.create_task(self.loop.sock_recv(self.connection, 1))
        resize = asyncio.create_task(self.watch_resize())
        tasks = {output, input_task, disconnected, resize}
        pending = set(tasks)
        try:
            while True:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if output in done:
                    return output.result()
                if disconnected in done:
                    disconnected.result()
                    return 255
                if resize in done:
                    resize.result()
                if input_task in done and input_task.result():
                    return 255
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class MuxServer:
    """Serve independent ssh clients over a shared, already-open Protocol.

    The socket is created with mode 0600, and an existing path is never removed.
    run() owns accepted sockets and removes only the socket it created.
    """

    motd_paths: tuple[Path, ...] = (Path("/run/motd.dynamic"), Path("/etc/motd"))

    def __init__(
        self,
        protocol: Protocol,
        path: str,
        idle_timeout: float = 0,
        *,
        agent_socket: str | None = None,
        motd: bool = False,
    ) -> None:
        self.protocol = protocol
        self.agent_path = os.environ.get("SSH_AUTH_SOCK", "") if agent_socket is None else agent_socket
        self.path = Path(path)
        self.launcher = launcher_source()
        self.motd_launcher = launcher_source(motd_paths=self.motd_paths if motd else ())
        self.stopping = asyncio.Event()
        self.ready = asyncio.Event()
        self.activity = asyncio.Event()
        self.idle_timeout = idle_timeout
        self.clients: dict[asyncio.Task[None], MuxClient] = {}

    async def accept(self, listener: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            connection, _ = await loop.sock_accept(listener)
            connection.setblocking(False)
            client = MuxClient(self, connection)
            task = asyncio.create_task(client.run())
            self.clients[task] = client
            task.add_done_callback(self.client_closed)
            self.activity.set()

    def client_closed(self, task: asyncio.Task[None]) -> None:
        self.clients.pop(task, None)
        self.activity.set()

    async def stop_when_idle(self) -> None:
        """Stop after the timeout with no connected clients, including quiet shells."""
        while True:
            self.activity.clear()
            if self.clients:
                await self.activity.wait()
                continue
            try:
                await asyncio.wait_for(self.activity.wait(), self.idle_timeout)
            except TimeoutError:
                self.stopping.set()
                return

    def resized(self) -> None:
        """SIGWINCH carries no session id; read each active terminal's size."""
        for client in self.clients.values():
            if client.key is not None and client.request is not None and client.request.tty:
                client.resize.set()

    async def run(self, ready: BinaryIO | None = None) -> None:
        loop = asyncio.get_running_loop()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity: tuple[int, int] | None = None
        tasks: list[asyncio.Task[object]] = []
        previous = signal.getsignal(signal.SIGWINCH)
        watching = False
        try:
            # bind is synchronous: no other asyncio task can observe this umask.
            mask = os.umask(0o177)
            try:
                listener.bind(str(self.path))
            finally:
                os.umask(mask)
            metadata = self.path.lstat()
            identity = metadata.st_dev, metadata.st_ino
            listener.listen(64)
            listener.setblocking(False)
            loop.add_signal_handler(signal.SIGWINCH, self.resized)
            watching = True
            accepting = asyncio.create_task(self.accept(listener))
            stopped = asyncio.create_task(self.stopping.wait())
            closed = asyncio.create_task(self.protocol.wait_closed())
            tasks = [accepting, stopped, closed]
            if self.idle_timeout:
                tasks.append(asyncio.create_task(self.stop_when_idle()))
            self.ready.set()
            if ready is not None:
                ready.write(b"1")
                ready.flush()
                ready.close()
            log.info("Listening on %s", self.path)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if closed in done and not self.stopping.is_set():
                raise ConnectionError("rmote transport closed")
        finally:
            listener.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            clients = list(self.clients)
            for task in clients:
                task.cancel()
            await asyncio.gather(*clients, return_exceptions=True)
            if watching:
                loop.remove_signal_handler(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, previous)
            if identity is not None:
                with contextlib.suppress(FileNotFoundError):
                    metadata = self.path.lstat()
                    if (metadata.st_dev, metadata.st_ino) == identity:
                        self.path.unlink()


async def serve(
    path: str,
    transport: list[str],
    python: str = "python3",
    *,
    idle_timeout: float = 0,
    ready: BinaryIO | None = None,
    agent_socket: str | None = None,
    motd: bool = False,
) -> None:
    """Connect once, then serve until stopped, disconnected or idle."""
    protocol = await Protocol.from_command(*transport, python=python)
    async with protocol:
        server = MuxServer(protocol, path, idle_timeout, agent_socket=agent_socket, motd=motd)
        loop = asyncio.get_running_loop()
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
        try:
            for number in previous:
                loop.add_signal_handler(number, server.stopping.set)
            await server.run(ready)
        finally:
            for number, handler in previous.items():
                loop.remove_signal_handler(number)
                signal.signal(number, handler)


def receive_packet(connection: socket.socket) -> Packet:
    """Read a bounded mux message from a socket with a caller-supplied timeout."""

    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = connection.recv(size - len(result))
            if not chunk:
                raise ConnectionError("Control socket closed during its health check")
            result.extend(chunk)
        return bytes(result)

    size = int(UINT32.unpack(exact(4))[0])
    if size < 4 or size > MAX_PACKET:
        raise ValueError("Invalid control socket response")
    return Packet(exact(size))


def running_pid(path: str) -> int | None:
    """Return a responsive mux server's PID, or None for an absent/stale socket.

    Other connection and protocol errors propagate: a live or unrelated socket
    must never be mistaken for an abandoned socket and removed.
    """
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        try:
            connection.connect(path)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return None
            raise
        connection.sendall(packet(Message.HELLO, 4))
        hello = receive_packet(connection)
        if (hello.uint32(), hello.uint32()) != (Message.HELLO, 4):
            raise ValueError("Socket does not speak OpenSSH mux version 4")
        while hello.offset < len(hello.data):
            hello.string()
            hello.string()
        connection.sendall(packet(Message.ALIVE_CHECK, 1))
        reply = receive_packet(connection)
        if (reply.uint32(), reply.uint32()) != (Message.ALIVE, 1):
            raise ValueError("Control socket failed its health check")
        pid = reply.uint32()
        reply.end()
        return pid


def open_private_file(path: str) -> int:
    """Open an owned regular file without following symlinks, with mode 0600."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            raise ValueError(f"Not an owned regular file: {path}")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_running(
    path: str,
    start: Callable[[BinaryIO], None],
) -> None:
    """Reuse a live server or start one detached, returning only after readiness.

    A persistent adjacent lock file serializes launchers. It is intentionally
    never unlinked: changing its inode would let concurrent callers bypass it.
    The socket selects the destination; options only apply to a new server.
    """
    path = os.path.abspath(os.path.expanduser(path))
    deadline = time.monotonic() + START_TIMEOUT
    with contextlib.ExitStack() as stack:
        lock = stack.enter_context(os.fdopen(open_private_file(path + ".lock"), "ab"))
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for startup lock: {path}.lock") from None
                time.sleep(0.05)

        target = Path(path)
        metadata = target.lstat() if target.exists() or target.is_symlink() else None
        if metadata is not None and (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()):
            raise ValueError(f"Not an owned UNIX socket: {path}")
        if running_pid(path) is not None:
            return
        if metadata is not None:
            # Only remove a refused socket whose identity still matches.
            with contextlib.suppress(FileNotFoundError):
                current = target.lstat()
                if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError(f"Socket changed during startup: {path}")
                target.unlink()

        logfile = stack.enter_context(os.fdopen(open_private_file(path + ".log"), "a+b", buffering=0))
        log_start = logfile.seek(0, os.SEEK_END)
        read_fd, write_fd = os.pipe()
        reader = stack.enter_context(os.fdopen(read_fd, "rb", buffering=0))
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            pid = os.fork()
        except BaseException:
            os.close(write_fd)
            raise
        if pid == 0:
            status = 1
            try:
                reader.close()
                lock.close()
                os.setsid()
                # The intermediate child is reaped by our launcher; the daemon
                # is adopted even if the caller uses this function from Python.
                if os.fork() != 0:
                    os._exit(0)
                with open(os.devnull, "rb") as null:
                    os.dup2(null.fileno(), 0)
                os.dup2(logfile.fileno(), 1)
                os.dup2(logfile.fileno(), 2)
                start(os.fdopen(write_fd, "wb", buffering=0))
                status = 0
            except BaseException as error:
                print(f"rmote sshmux: {error}", file=sys.stderr, flush=True)
            finally:
                # Do not unwind the launcher's contexts or flush inherited buffers.
                os._exit(status)
        os.close(write_fd)
        closed = False
        try:
            readable, _, _ = select.select([reader], [], [], max(0, deadline - time.monotonic()))
            if not readable:
                raise TimeoutError(f"Timed out starting mux server; see {path}.log")
            if reader.read(1) != b"1":
                closed = True
                end = logfile.seek(0, os.SEEK_END)
                logfile.seek(max(log_start, end - 8192))
                detail = logfile.read().decode(errors="replace").strip()
                raise ConnectionError(f"Mux server failed to start: {detail or 'closed before readiness'}")
        except BaseException:
            if not closed:
                # A failed child may have exited concurrently with a timeout.
                # Cleanup must not replace the useful startup error.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
            raise
        finally:
            os.waitpid(pid, 0)


def idle_seconds(value: str) -> float:
    """Parse a finite, non-negative idle timeout; zero disables it."""
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be a finite number of seconds >= 0")
    return result


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Register mux arguments and their handler on a subcommand parser."""
    parser.description = (
        "Open a local OpenSSH control socket backed by one rmote connection. "
        "Each ssh client gets its own remote shell or command session (Vty).\n"
        "By default the server stays in the foreground. --daemon starts it in the "
        "background, or reuses the server already listening on this socket.\n\n"
        "TRANSPORT is the command used to reach the target. rmote appends "
        "python3 -qui automatically. The target needs Python 3.11+, but no "
        "rmote installation or sshd when reached through Docker."
    )
    parser.epilog = (
        "Docker (start the server in one terminal):\n"
        "  python -m rmote sshmux --socket ~/.ssh/container.sock -- docker exec -i my-container\n"
        "  # Equivalent console script:\n"
        "  rmote sshmux --socket ~/.ssh/container.sock -- docker exec -i my-container\n\n"
        "Connect from another terminal:\n"
        "  ssh -S ~/.ssh/container.sock -o ProxyCommand=false container\n"
        "  ssh -S ~/.ssh/container.sock -o ProxyCommand=false container 'uname -a'\n"
        "  # Repeat the first command for another independent terminal.\n\n"
        "Use docker exec -i, WITHOUT -t; do not append python3 -qui yourself. "
        "The transport carries binary data through pipes; Vty creates the PTYs. "
        "The name 'container' is a label: the socket already selects the target. "
        "ProxyCommand=false prevents ssh from falling back to a network connection.\n\n"
        "Other transports:\n"
        "  rmote sshmux --socket ~/.ssh/server.sock -- ssh user@server\n"
        "  rmote sshmux --socket ~/.ssh/local.sock  # local Python process\n\n\n"
        "Start automatically from ~/.ssh/config (put before Host * defaults):\n\n"
        "  Host container\n"
        "      ControlPath ~/.ssh/container.sock\n"
        "      ControlMaster no\n"
        "      ProxyCommand false\n"
        "      ForwardAgent no\n"
        "      ForwardX11 no\n"
        "  Match originalhost container exec "
        '"rmote sshmux --daemon --idle-timeout 300 --socket ~/.ssh/container.sock -- docker exec -i my-container"\n'
        "  Match all\n\n"
        "Then run: ssh container\n\n"
        "The --daemon waits for readiness; concurrent starts share one server.\n"
        "Background logs: SOCKET.log. Startup lock: SOCKET.lock.\n"
        "Match exec also runs for ssh -G and ssh -O check/exit.\n\n"
        "Check or stop the server:\n"
        "  ssh -S ~/.ssh/container.sock -O check container\n"
        "  ssh -S ~/.ssh/container.sock -O exit container\n"
        "  Ctrl-C in the server terminal also closes all sessions and the socket.\n\n"
        "Supports shell/exec, PTYs, resize and exit status on POSIX. "
        "SFTP v3 uses remote Python file operations. "
        "Agent forwarding (-A) uses --agent-socket or the mux server's SSH_AUTH_SOCK. "
        "Port forwarding is not supported. X11 forwarding requests produce a warning; "
        "the shell still opens without X11 forwarding."
    )
    parser.set_defaults(__func__=run, __prog__=parser.prog)
    parser.add_argument("-S", "--socket", required=True, help="Local UNIX control socket (reused with --daemon).")
    parser.add_argument(
        "-d", "--daemon", action="store_true", help="Start in the background if needed, and wait for readiness."
    )
    parser.add_argument(
        "-i",
        "--idle-timeout",
        type=idle_seconds,
        default=0,
        metavar="SECONDS",
        help="Stop after this many seconds without clients; 0 keeps the server running.",
    )
    parser.add_argument("-p", "--python", default="python3", help="Remote Python executable.")
    parser.add_argument(
        "--motd",
        action="store_true",
        help="Show remote /run/motd.dynamic and /etc/motd before shells with a PTY; respect ~/.hushlogin.",
    )
    parser.add_argument(
        "--agent-socket",
        metavar="PATH",
        help="Local SSH agent socket; overrides SSH_AUTH_SOCK. Clients must request forwarding with -A.",
    )
    parser.add_argument("-v", "--debug", action="store_true", help="Log protocol activity to stderr.")
    parser.add_argument(
        "transport",
        nargs=argparse.REMAINDER,
        metavar="TRANSPORT",
        help="Command and arguments, e.g. docker exec -i my-container; omit for local Python.",
    )


def run(args: argparse.Namespace) -> int:
    """Serve an OpenSSH control socket using parsed command line arguments."""
    transport = list(args.transport)
    if transport and transport[0] == "--":
        transport.pop(0)
    path = os.path.abspath(os.path.expanduser(args.socket))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(name)s: %(message)s")

    def start(ready: BinaryIO | None = None) -> None:
        asyncio.run(
            serve(
                path,
                transport,
                args.python,
                idle_timeout=args.idle_timeout,
                ready=ready,
                agent_socket=args.agent_socket,
                motd=args.motd,
            )
        )

    try:
        if args.daemon:
            ensure_running(path, start)
        else:
            start()
    except KeyboardInterrupt:
        return 130
    except (OSError, ConnectionError, ValueError) as error:
        print(f"{args.__prog__}: {error}", file=sys.stderr)
        return 1
    return 0
