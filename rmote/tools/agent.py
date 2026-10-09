"""Private Unix listeners for forwarding an SSH agent over rmote."""

import asyncio
import atexit
import contextlib
import logging
import os
import socket
import tempfile
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar, cast
from uuid import uuid4

from rmote.protocol import Protocol, Tool


@dataclass
class AgentConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    reading: bool = False
    writing: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> None:
        self.writer.close()
        try:
            async with asyncio.timeout(1):
                await self.writer.wait_closed()
        except (OSError, TimeoutError):
            self.writer.transport.abort()


@dataclass
class AgentSession:
    directory: str = ""
    path: str = ""
    server: asyncio.AbstractServer | None = None
    connections: dict[str, AgentConnection] = field(default_factory=dict)
    pending: asyncio.Queue[str | None] = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    connecting: set[str] = field(default_factory=set)
    accepting: bool = False
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.server is not None:
            self.server.close()
        # Wake accept even if nobody ever connected. No more callbacks can add
        # connections after the closed flag is set.
        while not self.pending.empty():
            self.pending.get_nowait()
        self.pending.put_nowait(None)
        connections, self.connections = self.connections, {}
        try:
            await asyncio.gather(*(connection.close() for connection in connections.values()))
            if self.server is not None:
                await self.server.wait_closed()
        finally:
            for connection in connections.values():
                connection.writer.transport.abort()
            with contextlib.suppress(FileNotFoundError):
                if self.path:
                    os.unlink(self.path)
            with contextlib.suppress(FileNotFoundError):
                if self.directory:
                    os.rmdir(self.directory)


class Agent(Tool):
    """Forward agents between independently selected rmote endpoints.

    Methods run on the remote event loop. A session ID is required for every
    operation; connection IDs from another session are rejected. Call release
    after all outstanding start calls have completed, including on cancellation.
    """

    CHUNK_SIZE: ClassVar[int] = 64 * 1024
    MAX_CONNECTIONS: ClassVar[int] = 32
    _sessions: ClassVar[dict[str, AgentSession]] = {}

    @classmethod
    async def start(cls, session_id: str | None = None) -> str:
        """Create a session on this endpoint and return its ID."""
        session_id = uuid4().hex if session_id is None else session_id
        if session_id in cls._sessions:
            raise ValueError("Agent session already exists")
        cls._sessions[session_id] = AgentSession()
        return session_id

    @classmethod
    async def listen(cls, session_id: str) -> str:
        """Create a private listener on this endpoint and return its path."""
        session = cls._session(session_id)
        if session.path:
            raise ValueError("Agent session already has a listener")
        # Keep paths below the Unix socket pathname limit, including on macOS.
        session.directory = tempfile.mkdtemp(prefix="rmote-agent-", dir="/tmp")
        session.path = os.path.join(session.directory, "agent.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if (
                session.closed
                or len(session.connections) + len(session.connecting) >= cls.MAX_CONNECTIONS
                or session.pending.full()
            ):
                writer.close()
                return
            connection_id = uuid4().hex
            session.connections[connection_id] = AgentConnection(reader, writer)
            session.pending.put_nowait(connection_id)

        try:
            listener.bind(session.path)
            os.chmod(session.path, 0o600)
            listener.setblocking(False)
            server = await asyncio.start_unix_server(connected, sock=listener, limit=cls.CHUNK_SIZE)
            session.server = server
            if session.closed:
                server.close()
                await server.wait_closed()
                raise ConnectionError("Agent session closed during startup")
            return session.path
        except BaseException:
            listener.close()
            await cls.release(session_id)
            raise

    @classmethod
    async def connect(cls, session_id: str, path: str | None = None, connection_id: str | None = None) -> str:
        """Connect to an agent on this endpoint and return a connection ID.

        With path=None, read SSH_AUTH_SOCK on this endpoint. An explicit
        connection ID lets a caller clean up after a cancelled response.
        """
        session = cls._session(session_id)
        path = os.environ.get("SSH_AUTH_SOCK", "") if path is None else path
        if not path:
            raise OSError("SSH_AUTH_SOCK is not set on the agent endpoint")
        connection_id = uuid4().hex if connection_id is None else connection_id
        if connection_id in session.connections or connection_id in session.connecting:
            raise ValueError("Agent connection already exists")
        if len(session.connections) + len(session.connecting) >= cls.MAX_CONNECTIONS:
            raise OSError("Agent session connection limit reached")
        session.connecting.add(connection_id)
        try:
            async with asyncio.timeout(5):
                reader, writer = await asyncio.open_unix_connection(path, limit=cls.CHUNK_SIZE)
            connection = AgentConnection(reader, writer)
            if session.closed:
                await connection.close()
                raise ConnectionError("Agent session closed during connect")
            session.connections[connection_id] = connection
            return connection_id
        finally:
            session.connecting.discard(connection_id)

    @staticmethod
    @contextlib.asynccontextmanager
    async def forward(
        *,
        listener: Protocol | None = None,
        agent: Protocol | None = None,
        path: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Expose an agent through a private socket on the listener endpoint.

        Each endpoint is an open Protocol, or None for this Python process.
        The returned path belongs to the listener endpoint. Resolve path (or
        SSH_AUTH_SOCK when omitted) on the agent endpoint. Examples::

            async with Agent.forward(listener=remote) as remote_socket:
                ...  # local agent, remote listener
            async with Agent.forward(agent=remote) as local_socket:
                ...  # remote agent, local listener
            async with Agent.forward(listener=host_a, agent=host_b) as socket_on_a:
                ...  # agent on B, listener on A

        Call this context manager directly, like FileSync.upload. It manages
        session-owned Tool operations on both endpoints and releases both
        sessions on exit. The relay does not add an OpenSSH session binding.
        """
        forwarding = _Forwarding(listener, agent, path)
        try:
            yield await forwarding.start()
        finally:
            await forwarding.close()

    @classmethod
    def _session(cls, session_id: str) -> AgentSession:
        session = cls._sessions.get(session_id)
        if session is None or session.closed:
            raise ConnectionError("Unknown agent session")
        return session

    @classmethod
    def _connection(cls, session_id: str, connection_id: str) -> AgentConnection:
        connection = cls._session(session_id).connections.get(connection_id)
        if connection is None:
            raise ConnectionError("Unknown agent connection")
        return connection

    @classmethod
    async def accept(cls, session_id: str) -> AsyncGenerator[str, None]:
        session = cls._session(session_id)
        if session.server is None:
            raise ValueError("Agent session has no listener")
        if session.accepting:
            raise RuntimeError("Agent session already has an accept stream")
        session.accepting = True
        try:
            while not session.closed:
                connection_id = await session.pending.get()
                if connection_id is None:
                    return
                if connection_id in session.connections:
                    yield connection_id
        finally:
            session.accepting = False
            await cls.release(session_id)

    @classmethod
    async def output(cls, session_id: str, connection_id: str) -> AsyncGenerator[bytes, None]:
        connection = cls._connection(session_id, connection_id)
        if connection.reading:
            raise RuntimeError("Agent connection already has an output stream")
        connection.reading = True
        try:
            while data := await connection.reader.read(cls.CHUNK_SIZE):
                yield data
        finally:
            connection.reading = False

    @classmethod
    async def write(cls, session_id: str, connection_id: str, data: bytes) -> None:
        if len(data) > cls.CHUNK_SIZE:
            raise ValueError("Agent write exceeds chunk limit")
        connection = cls._connection(session_id, connection_id)
        async with connection.writing:
            connection.writer.write(data)
            await connection.writer.drain()

    @classmethod
    async def end_input(cls, session_id: str, connection_id: str) -> None:
        connection = cls._connection(session_id, connection_id)
        async with connection.writing:
            connection.writer.write_eof()
            await connection.writer.drain()

    @classmethod
    async def close(cls, session_id: str, connection_id: str) -> None:
        session = cls._sessions.get(session_id)
        if session is not None:
            connection = session.connections.pop(connection_id, None)
            if connection is not None:
                await connection.close()

    @classmethod
    async def release(cls, session_id: str) -> None:
        session = cls._sessions.pop(session_id, None)
        if session is not None:
            await session.close()


@atexit.register
def remove_sockets() -> None:
    # The owning interpreter may exit after losing the rmote transport, when
    # no release RPC can arrive and its event loop has already stopped.
    for session in list(Agent._sessions.values()):
        with contextlib.suppress(OSError):
            if session.path:
                os.unlink(session.path)
        with contextlib.suppress(OSError):
            if session.directory:
                os.rmdir(session.directory)


T = TypeVar("T")


class _Endpoint:
    """Apply the same Tool operations locally or through a Protocol."""

    def __init__(self, protocol: Protocol | None) -> None:
        self.protocol = protocol
        self.session_id = uuid4().hex

    async def call(self, method: Callable[..., Coroutine[Any, Any, T]], *args: Any) -> T:
        if self.protocol is None:
            return await method(self.session_id, *args)
        return await self.protocol(method, self.session_id, *args)

    async def create(self, method: Callable[..., Coroutine[Any, Any, T]], *args: Any) -> T:
        # Finish resource creation before the surrounding finally releases the
        # session. Cancellation of an RPC alone does not stop the remote call.
        task = asyncio.create_task(self.call(method, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    def stream(self, method: Callable[..., AsyncGenerator[T, None]], *args: Any) -> AsyncGenerator[T, None]:
        if self.protocol is None:
            return method(self.session_id, *args)
        return cast(AsyncGenerator[T, None], self.protocol(method, self.session_id, *args))

    async def release(self) -> None:
        with contextlib.suppress(ConnectionError, OSError, TimeoutError):
            async with asyncio.timeout(5):
                await self.call(Agent.release)


class _Forwarding:
    """Coordinate symmetric Tool endpoints; neither role assumes locality."""

    def __init__(self, listener: Protocol | None, agent: Protocol | None, path: str | None) -> None:
        self.listener = _Endpoint(listener)
        self.agent = _Endpoint(agent)
        self.path = path
        self.accepting: asyncio.Task[None] | None = None
        self.connections: set[asyncio.Task[None]] = set()

    async def start(self) -> str:
        await self.agent.create(Agent.start)
        # Check the agent endpoint before exposing the listener.
        probe = await self.agent.create(Agent.connect, self.path)
        await self.agent.call(Agent.close, probe)
        await self.listener.create(Agent.start)
        path = await self.listener.create(Agent.listen)
        self.accepting = asyncio.create_task(self.accept())
        return path

    async def accept(self) -> None:
        try:
            async with contextlib.aclosing(self.listener.stream(Agent.accept)) as stream:
                async for connection_id in stream:
                    task = asyncio.create_task(self.forward(connection_id))
                    self.connections.add(task)
                    task.add_done_callback(self.connections.discard)
        except (ConnectionError, OSError):
            logging.getLogger(__name__).debug("Agent accept stream closed", exc_info=True)
        except Exception:
            logging.getLogger(__name__).exception("Agent accept stream failed")

    async def forward(self, listener_id: str) -> None:
        agent_id = uuid4().hex
        tasks: list[asyncio.Task[None]] = []
        try:
            await self.agent.create(Agent.connect, self.path, agent_id)

            async def copy(source: _Endpoint, source_id: str, target: _Endpoint, target_id: str) -> None:
                async with contextlib.aclosing(source.stream(Agent.output, source_id)) as stream:
                    async for data in stream:
                        await target.call(Agent.write, target_id, data)
                await target.call(Agent.end_input, target_id)

            tasks = [
                asyncio.create_task(copy(self.listener, listener_id, self.agent, agent_id)),
                asyncio.create_task(copy(self.agent, agent_id, self.listener, listener_id)),
            ]
            await asyncio.gather(*tasks)
        except (ConnectionError, OSError, TimeoutError):
            logging.getLogger(__name__).debug("Agent connection closed", exc_info=True)
        except Exception:
            logging.getLogger(__name__).exception("Agent connection failed")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for endpoint, connection_id in ((self.listener, listener_id), (self.agent, agent_id)):
                with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                    async with asyncio.timeout(5):
                        await endpoint.call(Agent.close, connection_id)

    async def close(self) -> None:
        tasks = list(self.connections)
        if self.accepting is not None:
            tasks.append(self.accepting)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(self.listener.release(), self.agent.release())
