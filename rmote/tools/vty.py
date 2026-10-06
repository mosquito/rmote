"""Terminal and process sessions on the remote host.

A session with a terminal gives job control, a window size and full screen
programs. A session with pipes keeps the bytes exactly and reports a real end of
input, which suits a redirected input or output.

The output of a session travels through a streaming call, so the remote side
pushes the bytes as they appear and does not wait for a request per chunk.

The module needs a POSIX host, because a terminal comes from `pty` and
`termios`.
"""

import asyncio
import errno
import fcntl
import inspect
import os
import signal
import struct
import sys
import termios
import textwrap
from collections.abc import AsyncIterator
from typing import Any

from rmote.protocol import Tool


def default_shell() -> str:
    """Return the login shell of the current user, or /bin/sh."""
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    except (ImportError, KeyError, OSError):
        return "/bin/sh"


def child_options() -> dict[str, Any]:
    """Return the options that put a child in its own session.

    A new session is what lets the child take the terminal as its own.
    """
    return {"start_new_session": True}


async def end_process(process: asyncio.subprocess.Process) -> None:
    """Signal the child's process group and wait, with a deadline on each step.

    SIGHUP comes first, because a shell reads it as the end of its terminal.
    A child that ignores every signal must not hold the cleanup forever, so
    the last step also gives up. A process that left the process group of
    the session can survive, and this is not a promise to find it.
    """
    steps = ((signal.SIGHUP, 1.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0))
    for number, timeout in steps:
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout)
            return
        except TimeoutError:
            continue


def take_controlling_terminal() -> None:
    """Take the controlling terminal, then run the requested command.

    The function runs as a fresh interpreter after exec, so the process has
    one thread and Python is safe to use. A preexec_fn would instead run
    Python between fork and exec in a process that has threads, where the
    standard library warns about a deadlock.

    Descriptor 0 is the terminal, and the process is already a session
    leader. Without TIOCSCTTY a shell reports that job control is off.
    """
    import fcntl
    import os
    import sys
    import termios

    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError as error:
        sys.stderr.write(f"rmote: no controlling terminal: {error}\n")

    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    except OSError as error:
        sys.stderr.write(f"rmote: cannot run {sys.argv[1]}: {error}\n")
        raise SystemExit(127) from error


def launcher_source() -> str:
    """Return the launcher as source that ``python -c`` can run.

    The launcher has to be a fresh interpreter, so it travels as text. The
    text comes from the function itself, which keeps one definition that the
    linter and the type checker also read. A transferred module has no file,
    so only the side that holds the file can produce the text.
    """
    return textwrap.dedent(inspect.getsource(take_controlling_terminal)) + "\n\ntake_controlling_terminal()\n"


def set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the window size of a terminal descriptor."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def wait_readable(fd: int) -> None:
    """Wait until *fd* has bytes to read."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def ready() -> None:
        if not future.done():
            future.set_result(None)

    loop.add_reader(fd, ready)
    try:
        await future
    finally:
        loop.remove_reader(fd)


async def wait_writable(fd: int) -> None:
    """Wait until *fd* accepts more bytes."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def ready() -> None:
        if not future.done():
            future.set_result(None)

    loop.add_writer(fd, ready)
    try:
        await future
    finally:
        loop.remove_writer(fd)


async def write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data*.

    A terminal or a pipe can be full, and one write can be partial. The
    function waits for writability instead of a timer, so the event loop
    stays free.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            await wait_writable(fd)
            continue
        view = view[written:]


class Session:
    """A child process on the remote host, with the descriptors it needs.

    The contract holds no process object of its own, because a backend can run
    the child through an interface that gives none.
    """

    async def read(self, size: int) -> bytes:
        """Read up to *size* bytes of output. Empty bytes mean the end."""
        raise NotImplementedError

    async def read_stderr(self, size: int) -> bytes:
        """Read separate stderr, or EOF when it is merged into output."""
        return b""

    async def write(self, data: bytes) -> None:
        """Pass payload bytes to the child."""
        raise NotImplementedError

    async def end_input(self) -> None:
        """Tell the child that its input ended."""
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:
        """Apply a new window size. A session without a terminal has none."""

    async def release(self) -> None:
        """End the child and release the descriptors. Return when it is done."""
        raise NotImplementedError

    async def wait(self) -> int:
        """Wait for the child and return its exit status."""
        raise NotImplementedError

    @property
    def status(self) -> int | None:
        """Exit status of the child, or None while it runs."""
        raise NotImplementedError


class ProcessSession(Session):
    """A session whose child is an ordinary asyncio subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    async def release(self) -> None:
        if self.process.returncode is None:
            # Output EOF can arrive before asyncio records the child's exit.
            # Give the watcher time to reap it before sending a signal, which
            # can steal the exit status on older Python versions.
            try:
                await asyncio.wait_for(self.process.wait(), 0.1)
            except TimeoutError:
                await end_process(self.process)

    async def wait(self) -> int:
        return await self.process.wait()

    @property
    def status(self) -> int | None:
        return self.process.returncode


class PipeSession(ProcessSession):
    """A child process with ordinary pipes and no terminal.

    A pipe has a real half close, so the end of input reaches the child exactly.
    The bytes pass through without terminal processing, which keeps binary data
    and line endings as they are. This backend works on every platform.
    """

    async def read(self, size: int) -> bytes:
        assert self.process.stdout is not None, "stdout must be a pipe"
        return await self.process.stdout.read(size)

    async def read_stderr(self, size: int) -> bytes:
        if self.process.stderr is None:
            return b""
        return await self.process.stderr.read(size)

    async def write(self, data: bytes) -> None:
        stdin = self.process.stdin
        if stdin is None or stdin.is_closing():
            return
        try:
            stdin.write(data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The child closed its input. To drop the bytes gives the same
            # result as a write to a closed pipe.
            pass

    async def end_input(self) -> None:
        """Close the write end of the child's input. This is a real half close."""
        stdin = self.process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()

    async def release(self) -> None:
        await self.end_input()
        await super().release()


class TerminalSession(ProcessSession):
    """A child process behind a pseudo terminal."""

    def __init__(self, process: asyncio.subprocess.Process, master_fd: int, slave_fd: int) -> None:
        super().__init__(process)
        self.master_fd = master_fd
        self.slave_fd = slave_fd
        self.exit_waiter = asyncio.create_task(process.wait())
        # A done callback also runs when release cancels an unstarted task.
        self.exit_waiter.add_done_callback(lambda _: self.close_slave())

    def close_slave(self) -> None:
        if self.slave_fd >= 0:
            os.close(self.slave_fd)
            self.slave_fd = -1

    @classmethod
    async def start(
        cls,
        command: list[str],
        rows: int,
        cols: int,
        child_env: dict[str, str],
        cwd: str | None,
        launcher: str,
    ) -> Session:
        """Open a terminal and start the child on its slave side.

        The master descriptor is stored before anything can fail, so the
        session releases it on every path.
        """
        import pty

        master_fd, slave_fd = pty.openpty()
        try:
            os.set_blocking(master_fd, False)
            set_winsize(master_fd, rows, cols)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                launcher or launcher_source(),
                *command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=child_env,
                cwd=cwd,
                **child_options(),
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        # macOS flushes unread output on the last slave close. Retaining our
        # copy lets the controlling process drain its terminal during exit.
        # Once it exits, close the copy so the master can report EOF on Linux.
        return cls(process, master_fd, slave_fd)

    async def read(self, size: int) -> bytes:
        await wait_readable(self.master_fd)
        try:
            return os.read(self.master_fd, size)
        except OSError as error:
            # EIO means the child released the terminal, which is the normal
            # end of a session. Every other failure is real.
            if error.errno not in (errno.EIO, errno.EBADF):
                raise
            return b""

    async def write(self, data: bytes) -> None:
        await write_all(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if self.master_fd >= 0:
            set_winsize(self.master_fd, rows, cols)

    async def end_input(self) -> None:
        """Write the VEOF character of the terminal.

        A terminal has no half close. A reader in canonical mode treats VEOF
        as the end of its input. A program that set raw mode gets the
        character as ordinary data, so this is not a general half close.
        """
        await self.write(self.end_of_input_character())

    def end_of_input_character(self) -> bytes:
        """Return the VEOF character of the terminal, or Ctrl-D."""
        try:
            value = termios.tcgetattr(self.master_fd)[6][termios.VEOF]
        except (OSError, IndexError):
            return b"\x04"
        return value if isinstance(value, bytes) else bytes((value,))

    async def release(self) -> None:
        # Release the terminal first. The kernel then sends SIGHUP to the
        # session, and the descriptor is free even if the rest is stopped.
        if self.master_fd >= 0:
            loop = asyncio.get_running_loop()
            loop.remove_reader(self.master_fd)
            loop.remove_writer(self.master_fd)
            os.close(self.master_fd)
            self.master_fd = -1
        try:
            await super().release()
        finally:
            self.exit_waiter.cancel()
            self.close_slave()
            await asyncio.gather(self.exit_waiter, return_exceptions=True)


class Vty(Tool):
    """Start a command on the remote host and stream its output.

    Open a session, read its output, write to it, and close it. The output call
    is a streaming call, so the bytes arrive as the child writes them::

        key = await remote(Vty.open, ["/bin/sh"], want_pty=False)
        async for chunk in remote(Vty.output, key):
            handle(chunk)
        status = await remote(Vty.close, key)

    Close releases the terminal and the child on every path, so a caller must
    call it even after a failure.

    A session with pipes works on every platform. A session with a terminal
    needs a POSIX host for now.
    """

    # Largest chunk of output in one stream item.
    CHUNK_SIZE = 64 * 1024

    sessions: dict[int, Session] = {}
    last_key = 0

    @staticmethod
    async def open(
        argv: list[str] | None = None,
        rows: int = 24,
        cols: int = 80,
        term: str = "",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        want_pty: bool = True,
        launcher: str = "",
        separate_stderr: bool = False,
    ) -> int:
        """Start a command and return the key of its session.

        Args:
            argv: Command and arguments. None or an empty list starts the
                command processor of the remote host.
            rows: Terminal height in character cells.
            cols: Terminal width in character cells.
            term: Value of the TERM variable. An empty value keeps the
                environment of the remote interpreter.
            env: Extra environment variables for the child.
            cwd: Working directory of the child. None keeps the remote default.
            want_pty: True gives the child a terminal, which needs a POSIX host.
                False gives it pipes, which work everywhere.
            launcher: Source of the helper that takes the controlling terminal.
                The caller produces it with launcher_source(), because a
                transferred module has no file of its own. An empty value makes
                this side produce it, which works only with the file present.
            separate_stderr: Keep stderr separate in pipe mode. Read it with
                stderr() concurrently with output() to avoid blocking the child.
                A terminal always combines stdout and stderr.

        Returns:
            The key of the session, for the other methods.

        Raises:
            OSError: The command cannot start.
            NotImplementedError: A terminal was asked for on a host that has no
                backend for one.
        """
        command = list(argv or []) or [default_shell()]

        child_env = dict(os.environ)
        child_env.update(env or {})
        if term:
            child_env["TERM"] = term

        session: Session
        if want_pty:
            session = await TerminalSession.start(command, rows, cols, child_env, cwd, launcher)
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if separate_stderr else asyncio.subprocess.STDOUT,
                env=child_env,
                cwd=cwd,
                **child_options(),
            )
            session = PipeSession(process)

        Vty.last_key += 1
        Vty.sessions[Vty.last_key] = session
        return Vty.last_key

    @staticmethod
    async def shell(
        command: str = "",
        *,
        rows: int = 24,
        cols: int = 80,
        term: str = "",
        env: dict[str, str] | None = None,
        want_pty: bool = True,
        launcher: str = "",
    ) -> int:
        """Run a command through the remote user's shell, or open a login shell.

        Pipe sessions keep stderr separate; read stderr() alongside output().
        All other arguments have the same meaning as in open().
        """
        shell = default_shell()
        argv = [shell, "-c", command] if command else [shell, "-l"]
        return await Vty.open(
            argv,
            rows=rows,
            cols=cols,
            term=term,
            env=env,
            want_pty=want_pty,
            launcher=launcher,
            separate_stderr=True,
        )

    @staticmethod
    async def output(key: int) -> AsyncIterator[bytes]:
        """Yield the output of the session until it ends.

        The stream ends when the child releases its output. The exit status
        comes from close().

        Args:
            key: Key that open() returned.

        Yields:
            Chunks of output, in the order the child wrote them.
        """
        session = Vty.sessions[key]
        while True:
            data = await session.read(Vty.CHUNK_SIZE)
            if not data:
                return
            yield data

    @staticmethod
    async def stderr(key: int) -> AsyncIterator[bytes]:
        """Yield separate stderr until EOF; empty for terminals or merged pipes."""
        session = Vty.sessions[key]
        while data := await session.read_stderr(Vty.CHUNK_SIZE):
            yield data

    @staticmethod
    async def write(key: int, data: bytes) -> None:
        """Pass payload bytes to the child."""
        await Vty.sessions[key].write(data)

    @staticmethod
    async def end_input(key: int) -> None:
        """Tell the child that its input ended, and keep reading its output."""
        await Vty.sessions[key].end_input()

    @staticmethod
    async def resize(key: int, rows: int, cols: int) -> None:
        """Report a new window size to the terminal of the session."""
        Vty.sessions[key].resize(rows, cols)

    @staticmethod
    async def wait(key: int) -> int:
        """Wait for the child and return its exit status."""
        return await Vty.sessions[key].wait()

    @staticmethod
    async def close(key: int) -> int:
        """Release the session and return the exit status of its child.

        Repeated calls are safe. A session that already ended reports its
        status again.
        """
        session = Vty.sessions.pop(key, None)
        if session is None:
            return 0
        try:
            await session.release()
        finally:
            status = session.status
        return 0 if status is None else int(status)
