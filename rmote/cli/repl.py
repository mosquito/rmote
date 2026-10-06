"""Python console with an open connection and inspectable interactive tools."""

import argparse
import ast
import asyncio
import code
import inspect
import linecache
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack, ExitStack, contextmanager
from types import CodeType, FrameType, FunctionType, ModuleType
from typing import Any, Never

import rmote
from rmote import tools
from rmote._runtime import _Runtime
from rmote.process import process
from rmote.protocol import Protocol, Tool, inline
from rmote.sync import Connection
from rmote.tools import facts
from rmote.tools.facts.schema import FactsData


class Console(code.InteractiveConsole):
    """Keep source for ToolMeta and optionally evaluate top-level await.

    The namespace must be the registered ``__main__`` module's dictionary.
    A single virtual source file gives classes absolute line numbers on every
    supported Python. No files are written to disk.
    """

    def __init__(self, namespace: dict[str, Any], runtime: _Runtime | None = None) -> None:
        super().__init__(namespace, filename="<rmote-repl>")
        self.runtime = runtime
        self.source = ""
        self.failed = False
        namespace["__file__"] = self.filename
        if runtime is not None:
            self.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

    def runsource(self, source: str, filename: str = "<input>", symbol: str = "single") -> bool:
        """Compile one block, preserving future flags and source line offsets."""
        if symbol not in ("single", "exec", "eval"):
            raise ValueError(f"Unknown compile mode: {symbol}")
        self.failed = False
        padded = "\n" * self.source.count("\n") + source
        try:
            compiled = self.compile(padded, self.filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            self.failed = True
            self.showsyntaxerror(self.filename)
            return False
        if compiled is None:
            return True
        self.source += source + "\n"
        linecache.cache[self.filename] = (len(self.source), None, self.source.splitlines(keepends=True), self.filename)
        self.runcode(compiled)
        return False

    async def evaluate(self, compiled: CodeType) -> None:
        result = FunctionType(compiled, self.locals)()
        if inspect.iscoroutine(result):
            await result

    def runcode(self, code: CodeType) -> None:
        """Run sync code in the caller, async code on the connection's loop."""
        try:
            if self.runtime is None:
                exec(code, self.locals)
            else:
                self.runtime.run(lambda: self.evaluate(code))
        except SystemExit:
            raise
        except BaseException:
            self.failed = True
            self.showtraceback()


def enable_completion(namespace: dict[str, Any], stack: ExitStack) -> None:
    """Enable standard readline editing, session history and name completion."""
    try:
        import readline
        import rlcompleter
    except ImportError:
        return
    previous = readline.get_completer()
    readline.set_completer(rlcompleter.Completer(namespace).complete)
    stack.callback(readline.set_completer, previous)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def banner(host: FactsData, asynchronous: bool) -> str:
    """Describe the remote host and show a call in the selected interface."""
    system, python = host["system"], host["python"]
    example = ("await " if asynchronous else "") + 'remote(facts.gather, sections=["cpu", "memory"])'
    return (
        f"Connected to {system.hostname}: {system.os} {system.architecture}, "
        f"{python.implementation} {python.version}\n"
        "Ready: remote, host (system/python facts), rmote, Tool, facts and built-in tools.\n"
        "Python runs locally; remote(...) calls run on the connected host.\n"
        f">>> {example}"
    )


def terminate(signum: int, frame: FrameType | None) -> Never:
    """Unwind the console's contexts on SIGTERM."""
    raise SystemExit(128 + signum)


@contextmanager
def wake_on_signals() -> Iterator[None]:
    """Wake readline while a Python SIGINT or SIGTERM handler is pending."""
    main_thread = threading.get_ident()
    stopped = False
    handled: dict[int, int] = dict.fromkeys((signal.SIGINT, signal.SIGTERM), 0)
    interrupt = signal.getsignal(signal.SIGINT)

    def handle(signum: int, frame: FrameType | None) -> None:
        nonlocal stopped
        handled[signum] += 1
        if signum == signal.SIGINT:
            if callable(interrupt):
                interrupt(signum, frame)
        elif not stopped:
            stopped = True
            terminate(signum, frame)

    with ExitStack() as stack:
        reader, writer = os.pipe()
        stack.callback(os.close, reader)
        stack.callback(os.close, writer)
        os.set_blocking(writer, False)
        previous_handler = signal.signal(signal.SIGTERM, handle)
        stack.callback(signal.signal, signal.SIGTERM, previous_handler)
        if callable(interrupt):
            signal.signal(signal.SIGINT, handle)
            stack.callback(signal.signal, signal.SIGINT, interrupt)
        previous_fd = signal.set_wakeup_fd(writer)
        stack.callback(signal.set_wakeup_fd, previous_fd)

        def wake_main() -> None:
            observed = dict.fromkeys(handled, 0)
            while not stopped:
                data = os.read(reader, 4096)
                for signum in handled:
                    if signum not in data or stopped or signal.getsignal(signum) is not handle:
                        continue
                    if handled[signum] != observed[signum]:
                        # This includes the byte from our own forwarded signal:
                        # its Python handler ran, so it needs no further wakeup.
                        observed[signum] = handled[signum]
                        continue
                    # Python handles signals in the main thread, but readline
                    # needs its blocking syscall interrupted first. Retry until
                    # the handler runs, including a signal just before select.
                    signal.pthread_kill(main_thread, signum)
                    time.sleep(0.01)

        thread = threading.Thread(target=wake_main, name="rmote-repl-signals", daemon=True)
        try:
            thread.start()
            yield
        finally:
            stopped = True
            try:
                os.write(writer, b"\0")
            except BlockingIOError:
                # A full pipe already wakes the reader.
                pass
            if thread.ident is not None:
                thread.join()


def run_console(transport: list[str], *, python: str, asynchronous: bool, command: str | None) -> int:
    """Own the connection, console namespace and source cache for one session."""
    with ExitStack() as stack:
        previous_signal = signal.signal(signal.SIGTERM, terminate)
        stack.callback(signal.signal, signal.SIGTERM, previous_signal)
        runtime = None
        remote: Connection | Protocol
        if asynchronous:
            runtime = _Runtime()
            stack.callback(runtime.close)
            connections = AsyncExitStack()
            stack.callback(runtime.run, connections.aclose)

            async def connect() -> tuple[Protocol, FactsData]:
                async with asyncio.timeout(30):
                    protocol = await Protocol.from_command(*transport, python=python, start_new_session=True)
                    await connections.enter_async_context(protocol)
                    host = await protocol(facts.gather, sections=["system", "python"], raw=False)
                    return protocol, host

            remote, host = runtime.run(connect)
        else:
            remote = stack.enter_context(Connection.from_command(*transport, python=python, start_new_session=True))
            host = remote(facts.gather, sections=["system", "python"], raw=False)

        main = ModuleType("__main__")
        main.__dict__.update(
            rmote=rmote,
            asyncio=asyncio,
            remote=remote,
            host=host,
            Tool=Tool,
            Protocol=Protocol,
            Connection=Connection,
            process=process,
            inline=inline,
            **{name: getattr(tools, name) for name in tools.__all__},
        )
        previous_main = sys.modules["__main__"]
        sys.modules["__main__"] = main
        stack.callback(sys.modules.__setitem__, "__main__", previous_main)
        console = Console(main.__dict__, runtime)
        stack.callback(linecache.cache.pop, console.filename, None)
        if command is not None or not sys.stdin.isatty():
            source = command if command is not None else sys.stdin.read()
            if console.runsource(source, symbol="exec"):
                print("Incomplete Python input", file=sys.stderr)
                return 1
            return int(console.failed)
        enable_completion(main.__dict__, stack)
        with wake_on_signals():
            console.interact(banner=banner(host, asynchronous), exitmsg="")
        return 0


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Register the REPL's options and subcommand handler."""
    parser.description = (
        "Open a Python REPL with a connected remote and host facts. "
        "Calls are synchronous by default; --async enables top-level await. "
        "The transport receives Python and -qui automatically."
    )
    parser.epilog = """Examples:
  rmote repl                                      # local Python subprocess
  rmote repl -- ssh -T server
  rmote repl -- docker exec -i my-container         # use -i, without -t
  rmote repl --async -- ssh -T server
  rmote repl -c 'print(host["system"].hostname)' -- ssh -T server

Inside the REPL:
  host["system"]                                  # collected at startup
  remote(facts.gather, sections=["cpu", "memory"])  # sync (default)
  await remote(facts.gather, sections=["cpu"])      # with --async
"""
    parser.set_defaults(__func__=run, __prog__=parser.prog)
    parser.add_argument(
        "-a", "--async", dest="asynchronous", action="store_true", help="Use Protocol and top-level await."
    )
    parser.add_argument("-p", "--python", help="Remote Python executable.")
    parser.add_argument(
        "-c", dest="command", metavar="CODE", help="Execute Python code and exit instead of prompting."
    )
    parser.add_argument("-v", "--debug", action="store_true", help="Log protocol activity to stderr.")
    parser.add_argument(
        "transport", nargs=argparse.REMAINDER, help="Transport command; omit for a local Python process."
    )


def run(args: argparse.Namespace) -> int:
    """Run the selected interface using the shared CLI's parsed arguments."""
    transport = list(args.transport)
    if transport and transport[0] == "--":
        transport.pop(0)
    python = args.python or ("python3" if transport else sys.executable)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    try:
        return run_console(transport, python=python, asynchronous=args.asynchronous, command=args.command)
    except KeyboardInterrupt:
        return 130
    except (OSError, ConnectionError, TimeoutError) as error:
        print(f"{args.__prog__}: {error}", file=sys.stderr)
        return 1
