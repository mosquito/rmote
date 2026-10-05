"""Local terminal client and the ``rmote-shell`` entry point.

The client bridges the local standard descriptors to a session that the Vty tool
starts on the remote host. The transport is any command that passes stdin and
stdout through without changing the bytes, for example ``ssh host``.

With a terminal on both local descriptors the session gets a pseudo terminal,
which gives job control, a window size and full screen programs. With a
redirected input or output the session gets pipes instead, which keep the bytes
exactly and report a real end of input.
"""

import argparse
import asyncio
import atexit
import logging
import os
import signal
import sys
from collections.abc import Callable
from types import FrameType

from rmote.protocol import Protocol
from rmote.tools.vty import Vty, launcher_source

# Shape of the value that termios.tcgetattr returns and tcsetattr accepts.
TerminalAttributes = list[int | list[bytes | int]]

DEFAULT_ESCAPE = "~"
CLOSED_MESSAGE = b"\r\nrmote-shell: session closed\r\n"

STDIN = 0
STDOUT = 1


def terminal_size(*fds: int) -> tuple[int, int]:
    """Return rows and columns of the first descriptor that is a terminal."""
    for fd in fds or (STDOUT,):
        try:
            size = os.get_terminal_size(fd)
        except OSError:
            continue
        return size.lines, size.columns
    return 24, 80


def regular_file(fd: int) -> bool:
    """True when *fd* is a regular file.

    A selector cannot watch a regular file, and such a file is always ready.
    """
    import stat

    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        return False


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
    """Write every byte of *data* to a local descriptor.

    The client keeps its own copy of this helper, because the Vty module is
    transferred to the remote host and has to stay self contained.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            await wait_writable(fd)
            continue
        view = view[written:]


async def read_available(fd: int, size: int) -> bytes:
    """Wait until *fd* is readable, then read up to *size* bytes.

    A regular file is always ready, and a selector cannot watch one, so a
    redirect from a file is read directly. To wait for it would fail with
    EINVAL.
    """
    if not regular_file(fd):
        await wait_readable(fd)
    try:
        return os.read(fd, size)
    except OSError:
        return b""


class NonBlocking:
    """Make a descriptor non-blocking and restore its original flags.

    The descriptor is shared with the rest of the session, so the flags must go
    back. Without O_NONBLOCK a full pipe or terminal would stop the whole event
    loop inside one write.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved: int | None = None

    def enter(self) -> None:
        """Add O_NONBLOCK, unless the descriptor is a regular file."""
        import fcntl

        if regular_file(self.fd):
            return
        try:
            self.saved = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, self.saved | os.O_NONBLOCK)
        except OSError:
            self.saved = None

    def restore(self) -> None:
        """Put the original flags back. Repeated calls are safe."""
        import fcntl

        if self.saved is None:
            return
        saved, self.saved = self.saved, None
        try:
            fcntl.fcntl(self.fd, fcntl.F_SETFL, saved)
        except OSError:
            pass


class TerminalMode:
    """Raw mode for the local terminal, with a restore that always runs.

    A terminal left in raw mode gives no echo and no line editing, and the user
    must repair the session by hand. The restore must therefore run on every
    exit path, including an exception and a signal.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved: TerminalAttributes | None = None

    @property
    def active(self) -> bool:
        """True while the terminal is in raw mode."""
        return self.saved is not None

    def enter(self) -> None:
        """Switch to raw mode. Do nothing when *fd* is not a terminal."""
        import termios
        import tty

        if not os.isatty(self.fd):
            return
        self.saved = termios.tcgetattr(self.fd)
        # TCSADRAIN keeps input that the user typed before the session was
        # ready. The default of tty.setraw is TCSAFLUSH, which discards it.
        tty.setraw(self.fd, termios.TCSADRAIN)

    def restore(self) -> None:
        """Put the saved settings back. Repeated calls are safe."""
        import termios

        if self.saved is None:
            return
        saved, self.saved = self.saved, None
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)
        except OSError:
            pass


def guard_terminal(*restore: Callable[[], None]) -> Callable[[], None]:
    """Run every *restore* function when the process does not leave normally.

    A ``finally`` block does not run when a signal stops the process, so
    SIGTERM, SIGHUP and interpreter shutdown need their own path. Call the
    returned function to release the handlers again.
    """

    def restore_all() -> None:
        for action in restore:
            action()

    atexit.register(restore_all)
    previous: dict[int, Callable[[int, FrameType | None], None] | int | None] = {}

    def on_signal(signum: int, frame: FrameType | None) -> None:
        restore_all()
        earlier = previous.get(signum)
        if callable(earlier):
            earlier(signum, frame)
        raise SystemExit(128 + signum)

    for number in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[number] = signal.getsignal(number)
            signal.signal(number, on_signal)
        except (ValueError, OSError, AttributeError):
            previous.pop(number, None)

    def release() -> None:
        atexit.unregister(restore_all)
        for number, earlier in previous.items():
            try:
                signal.signal(number, earlier)
            except (ValueError, OSError, TypeError):
                pass

    return release


class EscapeFilter:
    """Recognize the closing sequence in the local input.

    In raw mode every key goes to the remote session, so Ctrl-C cannot stop the
    client. The escape character works only directly after a line end, the same
    rule as in ssh. ``<escape>.`` closes the session and ``<escape><escape>``
    sends one literal escape character.
    """

    def __init__(self, escape: bytes) -> None:
        self.escape = escape
        self.at_line_start = True
        self.pending = False

    def feed(self, data: bytes) -> tuple[bytes, bool]:
        """Split *data* into bytes to forward and a request to close."""
        if not self.escape:
            return data, False

        out = bytearray()
        for byte in data:
            char = bytes((byte,))
            if self.pending:
                self.pending = False
                if char == b".":
                    return bytes(out), True
                if char == self.escape:
                    out += self.escape
                else:
                    out += self.escape + char
                self.at_line_start = char in (b"\r", b"\n")
                continue

            if char == self.escape and self.at_line_start:
                self.pending = True
                continue

            out += char
            self.at_line_start = char in (b"\r", b"\n")
        return bytes(out), False


class ShellSession:
    """Bridge between the local descriptors and one remote session."""

    def __init__(self, protocol: Protocol, key: int, escape: bytes = b"") -> None:
        self.protocol = protocol
        self.key = key
        self.loop = asyncio.get_running_loop()
        self.filter = EscapeFilter(escape)
        self.closed_by_user = False

    async def pump_output(self) -> None:
        """Write remote output to stdout until the session ends."""
        async for chunk in self.protocol(Vty.output, self.key):
            await write_all(STDOUT, chunk)

    async def pump_input(self) -> None:
        """Forward local input, then report the end of it.

        The end of the local input is not the end of the session. The child can
        still write, so the session keeps reading its output.
        """
        while True:
            data = await read_available(STDIN, Vty.CHUNK_SIZE)
            if not data:
                break

            forward, closing = self.filter.feed(data)
            if forward:
                await self.protocol(Vty.write, self.key, forward)
            if closing:
                self.closed_by_user = True
                return

        await self.protocol(Vty.end_input, self.key)

    async def send_resize(self) -> None:
        """Report the current local window size to the remote terminal."""
        rows, cols = terminal_size(STDOUT, STDIN)
        try:
            await self.protocol(Vty.resize, self.key, rows, cols)
        except (ConnectionError, OSError, KeyError):
            pass

    def watch_resize(self) -> bool:
        """Report every later window change. Return False when unsupported."""
        try:
            self.loop.add_signal_handler(
                signal.SIGWINCH,
                lambda: self.loop.create_task(self.send_resize()),
            )
        except (NotImplementedError, AttributeError, ValueError):
            return False
        return True

    def stop_watching_resize(self) -> None:
        """Release the window change handler."""
        try:
            self.loop.remove_signal_handler(signal.SIGWINCH)
        except (NotImplementedError, AttributeError, ValueError):
            pass

    async def run(self) -> None:
        """Run both pumps until the session or the local input ends."""
        self.watch_resize()
        output_task = asyncio.ensure_future(self.pump_output())
        input_task = asyncio.ensure_future(self.pump_input())
        try:
            await asyncio.wait({output_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
            if output_task.done():
                output_task.result()
                return
            # The input pump finished first. Collect its failure, if any.
            input_task.result()
            if self.closed_by_user:
                return
            await output_task
        finally:
            output_task.cancel()
            input_task.cancel()
            await asyncio.gather(output_task, input_task, return_exceptions=True)
            self.stop_watching_resize()


async def run_shell(
    transport: list[str],
    command: list[str],
    python: str = "python3",
    term: str | None = None,
    escape: str = DEFAULT_ESCAPE,
    show_transport_stderr: bool = False,
    want_pty: bool = True,
) -> int:
    """Open a remote session over *transport* and bridge the local descriptors.

    A terminal on both local descriptors gives the session a pseudo terminal. A
    redirected input or output gives it pipes, with no raw mode and no escape
    sequence, so redirected bytes pass through unchanged.

    The remote child ends with the session. The escape sequence closes the
    session, it does not leave the child running.

    Args:
        transport: Transport command, for example ``["ssh", "server"]``. An
            empty list starts the interpreter on the local host.
        command: Command to run. An empty list starts the login shell of the
            remote user.
        python: Python executable to start at the far end.
        term: Value of TERM for the remote child. None copies the local value.
        escape: Escape character for the closing sequence. An empty string
            disables it. It applies to a terminal session only.
        show_transport_stderr: Pass the stderr of the transport command through,
            so messages from ssh stay visible while the session runs. Without
            it, that stream is read by the connection, and its last lines
            appear in the error of a start that fails.
        want_pty: False asks for pipes even when the local descriptors are a
            terminal. A host without a pseudo terminal needs that.

    Returns:
        Exit status of the remote child.

    Raises:
        NotImplementedError: The remote host has no terminal to give.
    """
    interactive = want_pty and os.isatty(STDIN) and os.isatty(STDOUT)
    rows, cols = terminal_size(STDOUT, STDIN)
    # Without the flag the stderr of the transport is a pipe that the
    # connection reads: its last lines explain a start that fails, instead of
    # a hang with no reason.
    stderr = sys.stderr.fileno() if show_transport_stderr else asyncio.subprocess.PIPE

    mode = TerminalMode(STDIN)
    flags = [NonBlocking(STDIN), NonBlocking(STDOUT)]
    release = guard_terminal(mode.restore, *(item.restore for item in flags))

    try:
        protocol = await Protocol.from_command(*transport, python=python, stderr=stderr)
        async with protocol:
            key = await protocol(
                Vty.open,
                list(command),
                rows=rows,
                cols=cols,
                term=term or os.environ.get("TERM", "xterm"),
                want_pty=interactive,
                launcher=launcher_source(),
            )
            session = ShellSession(protocol, key, escape.encode()[:1] if interactive else b"")
            if interactive:
                mode.enter()
            for item in flags:
                item.enter()
            try:
                await session.run()
            finally:
                for item in flags:
                    item.restore()
                mode.restore()
                status = await protocol(Vty.close, key)

            if session.closed_by_user:
                os.write(STDOUT, CLOSED_MESSAGE)
            return int(status)
    finally:
        for item in flags:
            item.restore()
        mode.restore()
        release()


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser of ``rmote-shell``."""
    parser = argparse.ArgumentParser(
        prog="rmote-shell",
        description=(
            "Open an interactive shell on a host. The transport command starts a "
            "Python interpreter at the far end, and rmote runs a session inside it."
        ),
        epilog=(
            "Examples: rmote-shell ssh server | "
            "rmote-shell docker exec -i container | "
            "rmote-shell --command /bin/bash --command=-l ssh server"
        ),
    )
    parser.add_argument(
        "--python",
        default="python3",
        help="Python executable to start at the far end (default: python3).",
    )
    parser.add_argument(
        "--command",
        "-c",
        action="append",
        default=[],
        metavar="ARG",
        help="Run this command instead of the login shell. Repeat it for each argument.",
    )
    parser.add_argument(
        "--term",
        default=None,
        help="Value of TERM for the remote child (default: the local TERM).",
    )
    parser.add_argument(
        "--escape",
        default=DEFAULT_ESCAPE,
        help="Escape character for the closing sequence <escape>. An empty value disables it.",
    )
    parser.add_argument(
        "--no-pty",
        action="store_true",
        help="Use pipes instead of a terminal, for the times when exact bytes matter more.",
    )
    parser.add_argument(
        "--transport-stderr",
        action="store_true",
        help="Show the stderr of the transport command, for example ssh messages.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Write protocol debug logs to stderr.",
    )
    parser.add_argument(
        "transport",
        nargs=argparse.REMAINDER,
        metavar="TRANSPORT",
        help="Transport command and its arguments, for example: ssh server",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``rmote-shell`` console script."""
    args = build_parser().parse_args(argv)
    transport = list(args.transport)
    # argparse keeps a leading separator in the remainder.
    if transport and transport[0] == "--":
        transport.pop(0)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

    try:
        return asyncio.run(
            run_shell(
                transport,
                list(args.command),
                python=args.python,
                term=args.term,
                escape=args.escape,
                show_transport_stderr=args.transport_stderr,
                want_pty=not args.no_pty,
            )
        )
    except KeyboardInterrupt:
        return 130
    except NotImplementedError as error:
        # The host has no terminal. Pipes still work, so name the option.
        print(f"rmote-shell: {error}", file=sys.stderr)
        print("rmote-shell: run it again with --no-pty to use pipes.", file=sys.stderr)
        return 1
    except (OSError, ConnectionError) as error:
        print(f"rmote-shell: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
