import asyncio
import base64
import enum
import inspect
import io
import logging
import os
import pickle
import re
import struct
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable, Coroutine
from functools import cache
from lzma import compress, decompress
from pathlib import Path
from types import FunctionType
from typing import Any, ParamSpec, Self, TypedDict, TypeVar, cast, overload


class Template:
    """A Mako-like template pre-compiled to a reusable render function.

    Picklable - the instance stores only the original template string, so it
    can be passed as an argument to remote tool calls over the protocol.  The
    class lives in ``protocol.py`` and is therefore available on the remote
    side without any extra sync step.

    Usage::

        tmpl = Template("Hello, ${name}!")
        tmpl.render(name="Alice")          # local
        await protocol(MyTool.run, tmpl)   # pass to remote tool

    Syntax (Mako-like):

    * ``${expr}``    - evaluate *expr* and insert the string result;
                       nested braces are handled correctly (e.g. ``${{'k': 1}['k']}``)
    * ``\\${``       - literal ``${`` (escape, no interpolation)
    * ``% stmt``     - Python control-flow line (for / if / while / …)
    * ``% endfor`` / ``% endif`` / ``% end``  - block terminators
    * ``%%``         - literal ``%`` at the start of an output line
    * ``## comment`` - ignored
    """

    BLOCK_OPEN = frozenset({"for", "if", "while", "with", "try", "def", "class"})
    BLOCK_CONT = frozenset({"else", "elif", "except", "finally"})
    BLOCK_END = frozenset({"endfor", "endif", "endwhile", "endwith", "end"})

    def __init__(self, template: str) -> None:
        self._template = template
        self._fn: Callable[..., str] = Template.compile(template)

    @staticmethod
    def _split_exprs(line: str) -> list[tuple[bool, str]]:
        """Split *line* into ``(is_expr, fragment)`` pairs.

        Handles ``\\${`` escape (→ literal ``${``), bare ``$`` (literal),
        and nested ``{}`` inside expressions via brace-depth counting.
        """
        result: list[tuple[bool, str]] = []
        buf: list[str] = []
        i = 0
        n = len(line)
        while i < n:
            # \${ → literal ${
            if line[i] == "\\" and line[i + 1 : i + 3] == "${":
                buf.append("${")
                i += 3
            # ${ → start of expression
            elif line[i] == "$" and i + 1 < n and line[i + 1] == "{":
                if buf:
                    result.append((False, "".join(buf)))
                    buf = []
                i += 2  # consume '${'
                depth = 1
                expr: list[str] = []
                while i < n and depth > 0:
                    ch = line[i]
                    if ch == "{":
                        depth += 1
                        expr.append(ch)
                    elif ch == "}":
                        depth -= 1
                        if depth > 0:
                            expr.append(ch)
                    else:
                        expr.append(ch)
                    i += 1
                result.append((True, "".join(expr)))
            else:
                buf.append(line[i])
                i += 1
        if buf:
            result.append((False, "".join(buf)))
        return result

    @staticmethod
    @cache
    def compile(template: str) -> "Callable[..., str]":
        """Compile a Mako-like *template* string into a reusable render function.

        Returns a callable that accepts ``**ctx`` keyword arguments and returns
        the rendered string.  Results are cached so repeated
        calls with the same template string are free.
        """
        indent_level = 0
        indent_unit = "    "
        lines_out = ["_out_ = []", "_pending_nl_ = False"]

        def cur_indent() -> str:
            return indent_unit * indent_level

        def emit_text_line(raw_line: str) -> None:
            lines_out.append(f"{cur_indent()}if _pending_nl_: _out_.append('\\n')")
            for is_expr, text in Template._split_exprs(raw_line):
                if is_expr:
                    lines_out.append(f"{cur_indent()}_out_.append(str({text}))")
                elif text:
                    lines_out.append(f"{cur_indent()}_out_.append({text!r})")
            lines_out.append(f"{cur_indent()}_pending_nl_ = True")

        for raw_line in template.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("##"):
                continue
            if stripped.startswith("%%"):
                # %% → literal % line (still processes ${} expressions)
                idx = raw_line.index("%%")
                emit_text_line(raw_line[:idx] + "%" + raw_line[idx + 2 :])
                continue
            if stripped.startswith("%"):
                code = stripped[1:].strip()
                if not code:
                    indent_level = max(0, indent_level - 1)
                    continue
                keyword = code.split()[0].rstrip(":(")
                if keyword in Template.BLOCK_END:
                    indent_level = max(0, indent_level - 1)
                elif keyword in Template.BLOCK_CONT:
                    indent_level = max(0, indent_level - 1)
                    py_line = code if code.endswith(":") else code + ":"
                    lines_out.append(f"{cur_indent()}{py_line}")
                    indent_level += 1
                elif keyword in Template.BLOCK_OPEN:
                    py_line = code if code.endswith(":") else code + ":"
                    lines_out.append(f"{cur_indent()}{py_line}")
                    indent_level += 1
                else:
                    lines_out.append(f"{cur_indent()}{code}")
                continue
            emit_text_line(raw_line)

        lines_out.append("_result_ = ''.join(_out_)")
        source = "\n".join(lines_out)
        code_obj = compile(source, "<template>", "exec")  # noqa: PLC0415

        def _render(**ctx: object) -> str:
            ns: dict[str, object] = dict(ctx)
            exec(code_obj, ns)
            return str(ns["_result_"])

        return _render

    def render(self, **ctx: object) -> str:
        """Render this template with *ctx* as the variable namespace."""
        return self._fn(**ctx)

    def __reduce__(self) -> tuple[type, tuple[str]]:
        return (Template, (self._template,))

    def __repr__(self) -> str:
        preview = self._template[:40].replace("\n", "\\n")
        return f"Template({preview!r})"


def render_template(template: str, **ctx: object) -> str:
    """Compile and render a Mako-like *template* string in one step."""
    return Template(template).render(**ctx)


class RPCRequest(TypedDict):
    method: str
    args: Any  # Can be tuple[Any, ...] or P.args
    kwargs: dict[str, Any]


class LogRecord(TypedDict):
    name: str
    levelno: int
    levelname: str
    pathname: str
    lineno: int
    msg: str
    args: Any
    exc_info: Any


def bootstrap_packer(code: bytes) -> bytes:
    with io.BytesIO() as output:
        output.write(b"from lzma import decompress\n")
        output.write(b"from base64 import b64decode\n")
        output.write(b"\n")
        output.write(b"exec(decompress(b64decode('''")
        output.write(base64.b64encode(compress(code)))
        output.write(b"''')))\n")
        return output.getvalue()


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

    Tools must be use only this function for execute subprocesses,
    to avoid conflicts with protocol communication.
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
        if isinstance(stdin, str):
            stdin = stdin.encode()
        # Use input= (not stdin=) so subprocess uses PIPE internally;
        # remove stdin=DEVNULL to avoid the "stdin and input may not both be used" error.
        del kwargs["stdin"]
        kwargs["input"] = stdin

    if not capture_output:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    return subprocess.run(cmd_and_args, **kwargs)


# Strip rmote imports except rmote.protocol, which is registered in sys.modules on the remote
_RMOTE_IMPORT_RE = re.compile(r"^(?:from|import) rmote(?!\.protocol\b)[^\n]*\n", re.MULTILINE)


class ToolMeta(type):
    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> type:
        cls = super().__new__(mcs, name, bases, namespace)

        if "__init__" in namespace:
            raise TypeError("__init__ cannot be defined in a Tool")

        # __source__ kept for inline-tool fallback (qualname contains <locals>)
        source: str | None = None
        try:
            source = textwrap.dedent(inspect.getsource(cls))
        except (OSError, TypeError):
            pass

        cls.__source__ = source  # type: ignore[attr-defined]
        cls.__class_name__ = name  # type: ignore[attr-defined]
        cls.__base_names__ = [b.__name__ for b in bases if b is not object]  # type: ignore[attr-defined]

        for attr_name in namespace:
            val = cls.__dict__.get(attr_name)
            if isinstance(val, FunctionType):
                val.__tool_class__ = cls  # type: ignore[attr-defined]
            elif isinstance(val, (staticmethod, classmethod)):
                val.__func__.__tool_class__ = cls  # type: ignore[union-attr]
            elif isinstance(val, property):
                for accessor in ("fget", "fset", "fdel"):
                    f = getattr(val, accessor, None)
                    if f:
                        f.__tool_class__ = cls

        return cls


class Tool(metaclass=ToolMeta):
    pass


def tool_to_dict(cls: type[Tool]) -> dict[str, Any]:
    name: str = cls.__class_name__  # type: ignore[attr-defined]

    # Inline/local tools (defined inside a function) - send class source only
    if "<locals>" in cls.__qualname__:
        return {"name": name, "source": cls.__source__ or ""}  # type: ignore[attr-defined]

    try:
        file_path = inspect.getfile(cls)
        source = _RMOTE_IMPORT_RE.sub("", Path(file_path).read_text())
        return {"name": name, "source": source, "module": cls.__module__, "file": file_path}
    except (TypeError, OSError):
        return {"name": name, "source": cls.__source__ or ""}  # type: ignore[attr-defined]


def tool_from_dict(data: dict[str, Any], context: dict[str, Any] | None = None) -> type[Tool]:
    import types as _types

    module_name: str | None = data.get("module")
    file_path: str = data.get("file", f"<transferred:{data['name']}>")

    if module_name and module_name not in sys.modules:
        module = _types.ModuleType(module_name)
        module.__file__ = file_path
        module.__dict__["__builtins__"] = __builtins__
        module.__dict__["Tool"] = Tool
        module.__dict__["process"] = process
        module.__dict__["render_template"] = render_template
        module.__dict__["Template"] = Template
        if context:
            module.__dict__.update(context)
        exec(compile(data["source"], file_path, "exec"), module.__dict__)
        sys.modules[module_name] = module
    elif not module_name:
        ctx: dict[str, Any] = {
            "__builtins__": __builtins__,
            "Tool": Tool,
            "process": process,
            "render_template": render_template,
            "Template": Template,
        }
        if context:
            ctx.update(context)
        exec(compile(data["source"], file_path, "exec"), ctx)
        return cast("type[Tool]", ctx[data["name"]])

    return cast("type[Tool]", getattr(sys.modules[module_name], data["name"]))


class Flags(enum.IntFlag):
    COMPRESSED = 1
    REQUEST = 1 << 1
    RESPONSE = 1 << 2
    SYNC = 1 << 3
    RPC = 1 << 4
    EXCEPTION = 1 << 5
    LOG = 1 << 6


class BaseProtocol:
    # 4 byte magic RMOTE, uint32 payload length
    MAGIC = b"RMOTE"
    BOUNDARY = b"PROTOCOL READY\n"
    PACKET_HEADER = struct.Struct(">5sIIQ")
    COMPRESSION_THRESHOLD = 1024

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.read_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()

    async def receive(self) -> tuple[Any, Flags, int]:
        async with self.read_lock:
            header = await self.reader.readexactly(self.PACKET_HEADER.size)
            magic, flags, length, packet_id = self.PACKET_HEADER.unpack(header)
            flags = Flags(flags)
            if magic != self.MAGIC:
                raise ValueError("Invalid magic number")
            payload = await self.reader.readexactly(length)
            if flags & Flags.COMPRESSED:
                payload = await asyncio.to_thread(decompress, payload)
            return pickle.loads(payload), flags, packet_id

    async def send(self, packet: Any, flags: Flags, packet_id: int) -> None:
        flags = Flags(flags)
        if flags & Flags.COMPRESSED:
            raise ValueError("Compression flag must not be set for send()")

        async with self.write_lock:
            payload = pickle.dumps(packet)
            if len(payload) > self.COMPRESSION_THRESHOLD:
                payload = await asyncio.to_thread(compress, payload)
                flags |= Flags.COMPRESSED
            header = self.PACKET_HEADER.pack(self.MAGIC, flags, len(payload), packet_id)
            self.writer.write(header + payload)
            await self.writer.drain()

    async def write_boundary(self) -> None:
        async with self.write_lock:
            self.writer.write(self.BOUNDARY)
            await self.writer.drain()

    async def read_boundary(self) -> None:
        async with self.read_lock:
            while True:
                try:
                    line = await self.reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # Line exceeded the stream reader limit - readline() consumed
                    # the oversized chunk and re-raised as ValueError (3.14+) or
                    # LimitOverrunError (older). Either way, skip and keep scanning.
                    continue
                if line == self.BOUNDARY:
                    return
                if not line:
                    raise ConnectionError("Remote process closed the connection before PROTOCOL READY")

    @classmethod
    async def from_subprocess(cls, process: asyncio.subprocess.Process) -> Self:
        assert process.stdin is not None, "Process stdin must not be None"
        assert process.stdout is not None, "Process stdout must not be None"
        process.stdin.write(bootstrap_packer(open(__file__, "rb").read()))
        process.stdin.write(b"asyncio.run(run())\n")
        return cls(reader=process.stdout, writer=process.stdin)

    @classmethod
    async def from_ssh(
        cls,
        host: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity: str | None = None,
        python: str = "python3",
        ssh_options: list[str] | None = None,
        stderr: int = asyncio.subprocess.DEVNULL,
    ) -> Self:
        """Bootstrap a Protocol over SSH.

        Args:
            host: Remote host, optionally in ``user@host`` form.
            user: Remote username (``-l``).  Overrides any user embedded in *host*.
            port: SSH port (``-p``).
            identity: Path to an SSH identity file (``-i``).
            python: Python executable on the remote host.
            ssh_options: Extra arguments inserted before the host in the ``ssh`` command
                (e.g. ``["-o", "StrictHostKeyChecking=no"]``).
            stderr: Where to redirect remote stderr.  Defaults to ``DEVNULL``.
        """
        cmd: list[str] = ["ssh", "-T"]
        if user is not None:
            cmd += ["-l", user]
        if port is not None:
            cmd += ["-p", str(port)]
        if identity is not None:
            cmd += ["-i", identity]
        if ssh_options:
            cmd += ssh_options
        cmd += [host, python, "-qui"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )
        instance = await cls.from_subprocess(proc)
        instance._owned_process = proc  # type: ignore[attr-defined]
        return instance

    @classmethod
    async def from_stdio(cls, logging_level: int = logging.INFO) -> Self:
        loop = asyncio.get_running_loop()

        # Protect the protocol channel from accidental corruption.
        #
        # The remote process talks to the local side over its own fd 0 (stdin)
        # and fd 1 (stdout).  Any child process that inherits those descriptors,
        # or any code that calls print() / os.write(1, ...) / subprocess.run()
        # with default stdio, can silently inject bytes into the binary packet
        # stream and break the connection permanently.
        #
        # Fix: dup stdin/stdout to new fds, redirect 0/1/2 to /dev/null, then
        # hand the duped fds to asyncio.  The new fds are marked close-on-exec
        # so they are never passed to child processes at all.  From this point
        # on, the protocol pipe is completely unreachable from user code and
        # from any subprocess spawned by tool methods.
        proto_in_fd = os.dup(sys.stdin.fileno())
        proto_out_fd = os.dup(sys.stdout.fileno())
        os.set_inheritable(proto_in_fd, False)
        os.set_inheritable(proto_out_fd, False)

        devnull_r = os.open(os.devnull, os.O_RDONLY)
        devnull_w = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_r, 0)
        os.dup2(devnull_w, 1)
        os.dup2(devnull_w, 2)
        os.close(devnull_r)
        os.close(devnull_w)

        proto_in = os.fdopen(proto_in_fd, "rb", buffering=0)
        proto_out = os.fdopen(proto_out_fd, "wb", buffering=0)

        async def wrap_reader() -> asyncio.StreamReader:
            reader = asyncio.StreamReader()
            proto = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: proto, proto_in)
            return reader

        async def wrap_writer() -> asyncio.StreamWriter:
            transport, proto = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, proto_out)
            writer = asyncio.StreamWriter(transport, proto, None, loop)
            return writer

        protocol = cls(await wrap_reader(), await wrap_writer())
        log_handler = RemoteLogHandler(protocol, loop)  # type: ignore[arg-type]
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(log_handler)
        root_logger.setLevel(logging_level)
        return protocol


P = ParamSpec("P")
R = TypeVar("R")


class Protocol(BaseProtocol):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        super().__init__(reader, writer)
        self._tools_cache: set[type[Tool]] = set()
        self.futures: dict[int, asyncio.Future[Any]] = {}
        self.loop = asyncio.get_running_loop()
        self._tools_cache = set()
        self._loop_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.tools: dict[str, Tool] = dict()

        self._owned_process: asyncio.subprocess.Process | None = None

        # Sync ID generation for RPC/SYNC packets
        self.__last_id = 0
        self.__last_id_lock = threading.Lock()

        # Separate ID generation for LOG packets (use negative IDs to avoid conflicts)
        self.__last_log_id = 0
        self.__last_log_id_lock = threading.Lock()

    def get_id(self) -> int:
        with self.__last_id_lock:
            self.__last_id += 1
            return self.__last_id

    def get_log_id(self) -> int:
        with self.__last_log_id_lock:
            self.__last_log_id -= 1
            return self.__last_log_id

    async def __aenter__(self) -> Self:
        await self.write_boundary()
        await self.read_boundary()
        self._loop_task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._closed.set()
        if self._loop_task:
            self._loop_task.cancel()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(self._loop_task, *self._tasks, return_exceptions=True)
            self._loop_task = None
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
        if self._owned_process is not None:
            try:
                self._owned_process.terminate()
            except ProcessLookupError:
                pass
            await self._owned_process.wait()
            self._owned_process = None

    def _load_tool(self, tool_definition: dict[str, Any], _: int) -> None:
        tool_cls = tool_from_dict(tool_definition)
        module = tool_definition.get("module")
        key = f"{module}.{tool_cls.__name__}" if module else tool_cls.__name__
        self.tools[key] = tool_cls()
        logging.debug("Loaded tool %s", tool_definition)

    async def _handle_rpc_request(self, request: RPCRequest, _: int) -> Any:
        if "." not in request["method"]:
            raise ValueError("Invalid method name")
        tool_name, method_name = request["method"].rsplit(".", 1)
        tool = self.tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Tool {tool_name} not found")
        method = getattr(tool, method_name, None)
        if method is None:
            raise ValueError(f"Method {method_name} not found in tool {tool_name}")
        if not callable(method):
            raise ValueError(f"{method_name} is not callable in tool {tool_name}")

        if inspect.iscoroutinefunction(method):
            return await method(*request["args"], **request["kwargs"])

        return await asyncio.to_thread(method, *request["args"], **request["kwargs"])

    async def _handle_rpc_response(self, response: Any, packet_id: int) -> None:
        if packet_id not in self.futures:
            logging.warning("RPC response %r packet not found in futures", packet_id)
            return
        future = self.futures.pop(packet_id)
        future.set_result(response)

    async def _handle_exception(self, exception: Exception, packet_id: int) -> None:
        if packet_id not in self.futures:
            logging.warning("Exception response %r packet not found in futures: %s", packet_id, exception)
            return
        future = self.futures.pop(packet_id)
        future.set_exception(exception)

    @staticmethod
    async def _handle_log(record: LogRecord, _: int) -> None:
        logger = logging.getLogger(f"rmote.remote.{record['name']}")
        log_record = logging.LogRecord(
            name=record["name"],
            level=record["levelno"],
            pathname=record["pathname"],
            lineno=record["lineno"],
            msg=record["msg"],
            args=record["args"],
            exc_info=record["exc_info"],
        )
        logger.handle(log_record)

    def _execute(
        self, packet_id: int, flags: Flags, handler: Callable[..., Any], payload: Any, need_response: bool = False
    ) -> None:

        async def wrapper() -> None:
            nonlocal flags
            coro: asyncio.Task[Any] | asyncio.Task[Any]
            if inspect.iscoroutinefunction(handler):
                coro = asyncio.create_task(handler(payload, packet_id))
            else:
                coro = asyncio.create_task(asyncio.to_thread(handler, payload, packet_id))

            try:
                resp = await coro
                flags |= Flags.RESPONSE
            except Exception as e:
                resp = e
                flags = Flags.EXCEPTION | Flags.RESPONSE

            if need_response:
                await self.send(resp, flags, packet_id)

        task = asyncio.create_task(wrapper())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _loop(self) -> None:
        error: Exception | None = None
        try:
            while not self._closed.is_set():
                payload, flags, packet_id = await self.receive()
                logging.debug("Received packet %d with flags %r: %r", packet_id, flags, payload)

                need_response = bool(flags & Flags.REQUEST)

                if flags & Flags.SYNC and flags & Flags.REQUEST:
                    self._execute(packet_id, Flags.SYNC, self._load_tool, payload, need_response)
                elif flags & Flags.RPC and flags & Flags.REQUEST:
                    self._execute(
                        packet_id,
                        Flags.RPC,
                        self._handle_rpc_request,
                        RPCRequest(**payload),  # type: ignore[typeddict-item]
                        need_response,
                    )
                elif (flags & Flags.RPC and flags & Flags.RESPONSE) or (flags & Flags.SYNC and flags & Flags.RESPONSE):
                    self._execute(packet_id, Flags.RPC, self._handle_rpc_response, payload, need_response)
                elif flags & Flags.EXCEPTION and flags & Flags.RESPONSE:
                    self._execute(packet_id, Flags.EXCEPTION, self._handle_exception, payload, need_response)
                elif flags & Flags.LOG:
                    self._execute(packet_id, Flags.LOG, self._handle_log, LogRecord(**payload), False)  # type: ignore[typeddict-item]
        except Exception as e:
            error = e
        finally:
            self._closed.set()
            exc = error or ConnectionError("Remote process closed the connection")
            for future in self.futures.values():
                if not future.done():
                    future.set_exception(exc)
            self.futures.clear()

    async def _call(self, payload: Any, flags: Flags) -> Any:
        logging.debug("Call %r %s", flags, payload)
        packet_id = self.get_id()
        future = self.loop.create_future()
        self.futures[packet_id] = future
        await self.send(payload, flags, packet_id)
        return await future

    @overload
    async def __call__(self, tool: Callable[P, Coroutine[Any, Any, R]], *args: P.args, **kwargs: P.kwargs) -> R: ...

    @overload
    async def __call__(self, tool: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R: ...

    async def __call__(self, tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        tool_class = getattr(tool, "__tool_class__", None)
        # For classmethods/staticmethods, check __func__ if __tool_class__ not found on the method itself
        if tool_class is None and hasattr(tool, "__func__"):
            tool_class = getattr(tool.__func__, "__tool_class__", None)
        if tool_class is None:
            raise ValueError("Only methods of Tool classes can be called with call_tool()")
        if tool_class not in self._tools_cache:
            await self._call(tool_to_dict(tool_class), Flags.SYNC | Flags.REQUEST)
            self._tools_cache.add(tool_class)
        # Use module-qualified name for file-based tools to avoid collisions
        # when different modules define tools with the same class name.
        # Inline tools (qualname contains <locals>) use bare class name.
        if "<locals>" in tool_class.__qualname__:
            tool_key = tool_class.__name__
        else:
            tool_key = f"{tool_class.__module__}.{tool_class.__name__}"
        method_id = f"{tool_key}.{tool.__name__}"
        result: Any = await self._call(
            RPCRequest(method=method_id, args=args, kwargs=kwargs), Flags.RPC | Flags.REQUEST
        )
        return result


class RemoteLogHandler(logging.Handler):
    def __init__(self, protocol: Protocol, loop: asyncio.AbstractEventLoop, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.protocol = protocol
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        record_dict = LogRecord(
            name=record.name,
            levelno=record.levelno,
            levelname=record.levelname,
            pathname=record.pathname,
            lineno=record.lineno,
            msg=record.msg,
            args=record.args,
            exc_info=record.exc_info,
        )
        # LOG packets use negative IDs to avoid conflicts with RPC packet_ids
        self.loop.create_task(self.protocol.send(record_dict, Flags.LOG, self.protocol.get_log_id()))


async def run() -> None:
    """remote endpoint entry point, do not call directly"""
    import types as _types

    # Register this module as rmote.protocol so Tool files can do
    # `from rmote.protocol import Tool, process` on the remote side.
    _mod = _types.ModuleType("rmote.protocol")
    _mod.__dict__.update(globals())
    sys.modules.setdefault("rmote", _types.ModuleType("rmote"))
    sys.modules["rmote.protocol"] = _mod

    proto = await Protocol.from_stdio()
    async with proto:
        await proto.wait_closed()
