import ast
import asyncio
import base64
import copyreg
import enum
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import io
import logging
import os
import pickle
import struct
import sys
import textwrap
import threading
import tokenize
import traceback
import zlib
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Iterable,
    Sequence,
)
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache, lru_cache
from gzip import compress, decompress
from pathlib import Path
from types import FunctionType, MappingProxyType, ModuleType
from typing import Any, ClassVar, NotRequired, ParamSpec, Self, TypeAlias, TypedDict, TypeVar, cast, overload


class RPCRequest(TypedDict):
    method: str
    args: Any  # Can be tuple[Any, ...] or P.args
    kwargs: dict[str, Any]
    compressed: NotRequired[bool]


# One record on the wire: logger name, level, source path, line number, the
# message that the sender already formatted, and the traceback text when there
# is one. A tuple costs half of what a mapping of the same fields costs to
# decode, and a record travels in the response of every call that logs.
LogRecord: TypeAlias = tuple[str, int, str, int, str, str | None]


class LogDelivery:
    """Hand remote records to local handlers outside the protocol loop.

    A handler can take a long time: a file with fsync, a socket, a lock inside
    logging. The loop thread reads and writes every packet of the connection,
    so it must not wait for one. It leaves the records in a deque instead, and
    one worker thread delivers them in arrival order.

    The loop thread only appends, and it wakes the worker only while the worker
    sleeps. An append costs a tenth of what a queue put costs, and a steady
    rate of records keeps the worker awake, so the wake-up disappears as well.

    The deque is bounded. When it is full the records are counted and the loss
    is reported through the ``rmote.remote`` logger, because a peer that logs
    faster than the handlers accept must not exhaust memory.
    """

    LIMIT: ClassVar[int] = 10000
    JOIN_TIMEOUT: ClassVar[float] = 5.0

    def __init__(self, limit: int = LIMIT) -> None:
        self.limit = limit
        self.records: deque[LogRecord] = deque()
        self.wake = threading.Event()
        self.worker: threading.Thread | None = None
        self.lost = 0
        # True only while the worker waits for the event. An append then has
        # to wake it; otherwise the worker finds the record by itself.
        self.idle = False
        self.closing = False

    def deliver(self, records: Sequence[LogRecord]) -> None:
        """Leave *records* for the worker, and start it on the first batch.

        A record that no local handler accepts is dropped here, on the thread
        that reads the packet: the test costs a tenth of a microsecond, and it
        saves the hand-off to the worker and the logging.LogRecord behind it.

        A delivery that was stopped starts no new worker, because the
        connection that produced the records is gone.
        """
        records = [record for record in records if self.target(record[0]).isEnabledFor(record[1])]
        if not records:
            return
        room = max(self.limit - len(self.records), 0)
        if room < len(records):
            self.lost += len(records) - room
            records = records[:room]
        self.records.extend(records)
        if self.idle:
            self.wake.set()
        if self.worker is None and not self.closing:
            self.worker = threading.Thread(target=self.run, name="rmote-log-delivery", daemon=True)
            self.worker.start()

    def run(self) -> None:
        """Deliver the records that wait, and sleep while none do."""
        reported = 0
        while True:
            try:
                record = self.records.popleft()
            except IndexError:
                if self.closing:
                    return
                # Say that this thread sleeps, then look again: a record that
                # arrived in between found idle unset and woke nobody.
                self.idle = True
                if not self.records and not self.closing:
                    self.wake.wait()
                self.idle = False
                self.wake.clear()
                continue
            try:
                self.emit(record)
            except Exception:  # noqa: BLE001 - a handler must not stop delivery
                logging.getLogger("rmote.remote").exception("Local handler failed on a remote record")
            if self.lost > reported:
                lost, reported = self.lost - reported, self.lost
                logging.getLogger("rmote.remote").error(
                    "Dropped %d remote log records: the local delivery queue was full", lost
                )

    @staticmethod
    @lru_cache(maxsize=256)
    def target(name: str) -> logging.Logger:
        """Give the local logger of a remote name, looked up once per name."""
        return logging.getLogger(f"rmote.remote.{name}")

    @classmethod
    def emit(cls, record: LogRecord) -> None:
        """Pass one remote record to the local handlers of its logger.

        The level of the logger was tested when the record arrived, and it can
        change while the record waits. The handlers test it again themselves,
        so a change in either direction is handled.
        """
        name, levelno, pathname, lineno, message, traceback_text = record
        logger = cls.target(name)
        log_record = logging.LogRecord(
            name=name, level=levelno, pathname=pathname, lineno=lineno, msg=message, args=(), exc_info=None
        )
        log_record.exc_text = traceback_text
        logger.handle(log_record)

    def stop(self) -> None:
        """Deliver what waits, then stop the worker.

        Blocks, so a caller on the event loop thread hands this to a thread.
        """
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        self.closing = True
        self.wake.set()
        worker.join(self.JOIN_TIMEOUT)


# The bootstrap is built once and written to every connection, so it pays for
# a slower pass over the sources: level 6 costs 3.5 times less than level 9 and
# gives up 0.7 percent of the bytes.
BOOTSTRAP_LEVEL = 6


def bootstrap_packer(code: bytes) -> bytes:
    with io.BytesIO() as output:
        output.write(b"from gzip import decompress\n")
        output.write(b"from base64 import b64decode\n")
        output.write(b"\n")
        output.write(b"exec(decompress(b64decode('''")
        output.write(base64.b64encode(compress(code, BOOTSTRAP_LEVEL)))
        output.write(b"''')))\n")
        return output.getvalue()


@cache
def bootstrap_payload() -> bytes:
    """Return the lines that bring rmote.protocol into a bare interpreter.

    The answer depends only on the sources of this package, which do not change
    while the process runs, so it is built once: reading and compressing them
    again costs milliseconds of every connection.
    """
    root_source = Path(__file__).with_name("__init__.py").read_bytes()
    protocol_source = Path(__file__).read_bytes()
    source = (
        "import sys, types, asyncio\n"
        "from importlib.machinery import ModuleSpec\n"
        "_package = types.ModuleType('rmote')\n"
        "_package.__path__ = []\n"
        "_package.__package__ = 'rmote'\n"
        "_package.__spec__ = ModuleSpec('rmote', loader=None, is_package=True)\n"
        "sys.modules['rmote'] = _package\n"
        f"exec(compile({root_source!r}, '<rmote>', 'exec'), _package.__dict__)\n"
        "_protocol = types.ModuleType('rmote.protocol')\n"
        "_protocol.__file__ = '<rmote.protocol>'\n"
        "_protocol.__package__ = 'rmote'\n"
        "sys.modules['rmote.protocol'] = _package.protocol = _protocol\n"
        f"exec(compile({protocol_source!r}, '<rmote.protocol>', 'exec'), _protocol.__dict__)\n"
    )
    return bootstrap_packer(source.encode())


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


Method = TypeVar("Method")


def streaming_method(tool: Callable[..., Any]) -> bool:
    """True when *tool* is an async generator, also behind a descriptor.

    A static method or a class method hides the function in __func__.
    """
    return inspect.isasyncgenfunction(getattr(tool, "__func__", tool))


def inline(method: Method) -> Method:
    """Run a synchronous Tool method directly on the event loop, without a worker.

    A synchronous method runs in a worker thread, because a call that waits
    would stop the loop and with it every other call of the connection. The
    hand-off to that thread costs about 30 us, which is more than a method
    that only reads a field, formats a value or returns a constant.

    Mark such a method with this decorator, above or below ``staticmethod``.
    Use it only when the method cannot wait: no file, no socket, no lock held
    by code that waits, and no long computation.
    The decorator does not enforce this requirement or change direct local calls.

        >>> class Tiny(Tool):
        ...     @staticmethod
        ...     @inline
        ...     def answer() -> int:
        ...         return 42
    """
    function: Any = getattr(method, "__func__", method)
    if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
        raise TypeError("inline applies to synchronous methods; a coroutine already runs on the loop")
    function.__rmote_inline__ = True
    return method


def runs_on_loop(method: Callable[..., Any]) -> bool:
    """True when the method is marked for inline execution."""
    return bool(getattr(method, "__rmote_inline__", False))


def tool_to_dict(cls: type[Tool], known: Collection[str] = ()) -> dict[str, Any]:
    """Describe a file-based class by its module bundle, or an inline class by source.

    *known* names the modules the peer already holds. Every class of a package
    shares one bundle, so its sources travel once for a connection and the
    classes that follow carry only what is still missing.
    """
    module = inspect.getmodule(cls)
    name = cls.__name__
    if "<locals>" not in cls.__qualname__ and module is not None and module.__name__ != "__main__":
        bundle = ModuleBundle.build(module)
        return {
            "name": name,
            "qualname": cls.__qualname__,
            "module": module.__name__,
            "sources": {item: source for item, source in bundle.items() if item not in known},
        }
    source = cls.__source__ or ""  # type: ignore[attr-defined]
    dependencies, bindings = ModuleBundle.references(source, module, set(known))
    return {"name": name, "source": source, "dependencies": dependencies, "bindings": bindings}


def tool_key(definition: dict[str, Any]) -> str:
    """Give the name a tool definition is registered under, on either side.

    A definition that names a module keeps the module and the qualified name,
    so two modules can hold a class of the same name. An inline definition
    carries source and no module, so its own name is the key. Both sides take
    the key from the definition, and they cannot disagree about it.
    """
    if definition.get("kind") == "module":
        return str(definition.get("module"))
    module = definition.get("module")
    name = definition.get("qualname", definition["name"])
    return f"{module}.{name}" if module else str(name)


def tool_from_dict(data: dict[str, Any], context: dict[str, Any] | None = None) -> type[Tool]:
    """Restore file-based classes through normal imports; execute inline source separately."""
    if data.get("module"):
        value: Any = ModuleLoader.load(data["module"], data.get("sources", {}))
        for name in data.get("qualname", data["name"]).split("."):
            value = getattr(value, name)
        return cast("type[Tool]", value)

    ModuleLoader.install(data.get("dependencies", {}))
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "Tool": Tool,
        # An inline class names the decorator in its own source, and the source
        # is executed here rather than imported.
        "inline": inline,
    }
    namespace.update(context or {})
    for name, (module_path, qualname) in data.get("bindings", {}).items():
        value = importlib.import_module(module_path)
        for part in qualname.split("."):
            value = getattr(value, part)
        namespace[name] = value
    exec(compile(data["source"], f"<transferred:{data['name']}>", "exec"), namespace)
    return cast("type[Tool]", namespace[data["name"]])


def reduce_tool_class(cls: ToolMeta) -> Any:
    """Carry Tool definitions when classes are passed as RPC arguments."""
    return tool_from_dict, (tool_to_dict(cast(type[Tool], cls)),)


copyreg.pickle(ToolMeta, reduce_tool_class)


class ModuleSource(TypedDict):
    """One importable source file in a transferred bundle."""

    source: str
    file: str
    package: bool


class ModuleBundle:
    """Build a source bundle for a module, or a package and its descendants.

    A module can declare ``__tool_package__ = 'my_package'`` to include that
    package instead. Imports outside this boundary must exist on the target.
    Only regular filesystem packages and Python source files are supported.
    """

    @classmethod
    def build(cls, module: ModuleType, seen: set[str] | None = None) -> dict[str, ModuleSource]:
        """Return the bundle of *module*, through the cache when it can.

        A call without *seen* asks for a whole bundle, which depends only on
        the root and is therefore cached. A recursive call carries the modules
        its caller already has, so its result is partial and is not cached.

        The caller receives a copy, because the cached mapping is read-only.
        """
        if seen is None:
            return dict(cls.bundle(cls.root(module)))
        return cls.expand(module, seen)

    @classmethod
    def root(cls, module: ModuleType) -> ModuleType:
        """Resolve the package whose bundle carries *module*.

        Every module of one package resolves to the same root, so the cache
        below holds one entry for the package and not one for each caller.
        """
        root_name = getattr(module, "__tool_package__", module.__name__)
        if not isinstance(root_name, str) or not (
            module.__name__ == root_name or module.__name__.startswith(root_name + ".")
        ):
            raise ValueError("__tool_package__ must name the defining module or an ancestor package")
        return importlib.import_module(root_name)

    @classmethod
    @lru_cache(maxsize=128)
    def bundle(cls, module: ModuleType) -> MappingProxyType[str, ModuleSource]:
        """Cache the whole bundle of a root; restart after source edits.

        Treat the returned mapping and its source records as read-only.
        """
        return MappingProxyType(cls.expand(module, set()))

    @classmethod
    def expand(cls, module: ModuleType, seen: set[str]) -> dict[str, ModuleSource]:
        """Collect the sources of a root and of everything it references."""
        root = cls.root(module)
        if root.__name__ in seen:
            return {}
        seen.add(root.__name__)
        own_sources = cls.collect(root)
        sources = dict(own_sources)
        seen.update(own_sources)
        for dependency in getattr(root, "__tool_dependencies__", ()):
            sources.update(cls.build(importlib.import_module(dependency), seen))
        for name, source in own_sources.items():
            dependencies, _ = cls.references(source["source"], sys.modules.get(name), seen)
            sources.update(dependencies)
        return sources

    @staticmethod
    def declared_module(value: Any) -> ModuleType | None:
        if isinstance(value, ModuleType):
            return ModuleBundle.declaring(value)
        owner = value if isinstance(value, (type, FunctionType)) else type(value)
        return ModuleBundle.owner_module(owner)

    @staticmethod
    def declaring(module: ModuleType | None) -> ModuleType | None:
        """Return *module* when it declares a package to transfer."""
        if module is not None and ("__tool_package__" in vars(module) or "__tool_dependencies__" in vars(module)):
            return module
        return None

    @staticmethod
    @lru_cache(maxsize=2048)
    def owner_module(owner: Any) -> ModuleType | None:
        """Cache the declaring module of a class or a function.

        The answer depends only on the owner, and a module does not gain the
        declaration while the process runs. The cache therefore holds one
        entry for each type that a payload carries, not one for each value.
        """
        return ModuleBundle.declaring(sys.modules.get(getattr(owner, "__module__", "")))

    @classmethod
    def references(
        cls,
        source: str,
        module: ModuleType | None,
        seen: set[str] | None = None,
    ) -> tuple[dict[str, ModuleSource], dict[str, tuple[str, str]]]:
        """Find explicitly transferable dependencies referenced by Tool source."""
        sources: dict[str, ModuleSource] = {}
        bindings: dict[str, tuple[str, str]] = {}
        seen = set() if seen is None else seen

        def include(value: Any) -> bool:
            dependency = (
                inspect.getmodule(value)
                if isinstance(value, ToolMeta) and value is not Tool
                else cls.declared_module(value)
            )
            if dependency is None:
                return False
            sources.update(cls.build(dependency, seen))
            return True

        include(module)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name) and module is not None:
                value = vars(module).get(node.id)
                if value is not None and include(value) and hasattr(value, "__qualname__"):
                    bindings[node.id] = (value.__module__, value.__qualname__)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names: list[str | None] = (
                    [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
                )
                for name in names:
                    if name is None:
                        continue
                    imported = sys.modules.get(name)
                    if imported is None and name.startswith("rmote."):
                        imported = importlib.import_module(name)
                    if imported is not None:
                        include(imported)
                        if isinstance(node, ast.ImportFrom):
                            for alias in node.names:
                                include(getattr(imported, alias.name, None))
        return sources, bindings

    @staticmethod
    def source(path: Path, *, package: bool) -> ModuleSource:
        with tokenize.open(path) as stream:
            return ModuleSource(source=stream.read(), file=str(path), package=package)

    @classmethod
    @lru_cache(maxsize=128)
    def collect(cls, root: ModuleType) -> MappingProxyType[str, ModuleSource]:
        """Cache sources for the root resolved by build; restart after source edits.

        Treat the returned mapping and its source records as read-only.
        """
        root_name = root.__name__
        filename = getattr(root, "__file__", None)
        for loader in sys.meta_path:
            if isinstance(loader, ModuleLoader) and root_name in loader.sources:
                return MappingProxyType(
                    {
                        name: value
                        for name, value in loader.sources.items()
                        if name == root_name or name.startswith(root_name + ".")
                    }
                )
        if not filename or not filename.endswith(".py"):
            raise ValueError(f"Module {root_name} must have a Python source file")
        path = Path(filename).resolve()
        if not hasattr(root, "__path__"):
            return MappingProxyType({root_name: cls.source(path, package=False)})
        if path.name != "__init__.py" or len(list(root.__path__)) != 1:
            raise ValueError("Only regular single-directory packages can be transferred")
        sources = {root_name: cls.source(path, package=True)}
        directory = path.parent
        for child in sorted(directory.rglob("*.py")):
            if child == path:
                continue
            relative = child.relative_to(directory)
            if not child.resolve().is_relative_to(directory):
                raise ValueError(f"Source file escapes package directory: {child}")
            # Namespace subpackages and caches are outside the source contract.
            if any(
                not (directory.joinpath(*relative.parts[:i]) / "__init__.py").is_file()
                for i in range(1, len(relative.parts))
            ):
                continue
            parts = list(relative.with_suffix("").parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            name = ".".join([root_name, *parts])
            sources[name] = cls.source(child, package=is_package)
        return MappingProxyType(sources)


class ModuleLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import transferred sources in memory using Python's normal import locks.

    Sources are immutable within a remote interpreter. Failed modules follow
    ordinary import semantics: they can be retried; successful dependencies stay
    loaded. Parent packages outside the bundle are empty namespaces if absent.
    """

    lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self) -> None:
        self.sources: dict[str, ModuleSource] = {}

    def find_spec(
        self, fullname: str, path: object = None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        source = self.sources.get(fullname)
        if source is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=source["package"])

    def get_source(self, fullname: str) -> str:
        """Expose in-memory source for tracebacks and inspect.getsource."""
        return self.sources[fullname]["source"]

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        source = self.sources[module.__name__]
        module.__file__ = source["file"]
        exec(compile(source["source"], source["file"], "exec"), module.__dict__)

    @classmethod
    def install(cls, sources: dict[str, ModuleSource]) -> "ModuleLoader":
        with cls.lock:
            loader = next((item for item in sys.meta_path if isinstance(item, cls)), None)
            if loader is None:
                loader = cls()
                sys.meta_path.insert(0, loader)
            for name, source in sources.items():
                previous = loader.sources.get(name)
                if previous is not None and previous != source:
                    raise ValueError(f"Transferred module changed: {name}; open a new connection")
                existing = sys.modules.get(name)
                if existing is not None and previous is None:
                    filename = getattr(existing, "__file__", None)
                    try:
                        same_source = (
                            filename is not None
                            and ModuleBundle.source(Path(filename), package=hasattr(existing, "__path__"))["source"]
                            == source["source"]
                        )
                    except (OSError, UnicodeError):
                        same_source = False
                    if not same_source:
                        raise ValueError(f"Module already loaded outside this bundle: {name}")
            # Register everything before importing, so sibling and circular
            # imports use the same normal Python loading machinery.
            loader.sources.update(sources)
            for name in sources:
                parent = name.rpartition(".")[0]
                while parent:
                    if parent not in sources and parent not in sys.modules and parent not in loader.sources:
                        loader.sources[parent] = ModuleSource(source="", file=f"<namespace:{parent}>", package=True)
                    parent = parent.rpartition(".")[0]
            return loader

    @classmethod
    def load(cls, name: str, sources: dict[str, ModuleSource]) -> ModuleType:
        cls.install(sources)
        return importlib.import_module(name)


# Exact types that can never declare a package to transfer. Pickle asks about
# every object of a payload, and a list of numbers asks thousands of times, so
# the answer for these has to cost nothing. The name stays at module level
# because a lookup through the instance costs twice as much as the check
# itself. The test is on the exact type: a subclass declared in a transferable
# module still resolves.
PLAIN_TYPES: frozenset[type] = frozenset(
    {int, float, bool, complex, str, bytes, bytearray, type(None), list, tuple, dict, set, frozenset}
)


class ToolDispatch(dict[type, Any]):
    """Reduce Tool classes, and leave every other type to copyreg.

    A pickler that carries its own dispatch table replaces the global table of
    copyreg instead of extending it, so this one answers for the types copyreg
    knows as well. Only types that the built-in dispatch does not handle reach
    it, so a payload of builtin values never asks.
    """

    def __init__(self, reduce_tool: Callable[[Any], Any]) -> None:
        super().__init__()
        self.reduce_tool = reduce_tool

    def __missing__(self, key: type) -> Any:
        if isinstance(key, type) and issubclass(key, ToolMeta):
            return self.reduce_tool
        reduction = copyreg.dispatch_table.get(key)
        if reduction is None:
            raise KeyError(key)
        return reduction


class ModulePickler(pickle.Pickler):
    """Discover explicitly transferable modules in arguments and return values."""

    def __init__(self, stream: io.BytesIO, known: set[str]) -> None:
        # The table must be in place before the base class reads it.
        self.dispatch_table = ToolDispatch(self.reduce_tool)
        super().__init__(stream)
        self.known = known
        self.sources: dict[str, ModuleSource] = {}

    def reduce_tool(self, cls: Any) -> Any:
        """Carry a Tool class by its identity, with its bundle out of band.

        The sources travel in the MODULES envelope of the packet, and the peer
        installs them before it decodes the payload. A class whose modules the
        peer already holds therefore costs no more than its name. An inline
        class has no module to import and keeps its source in the payload.
        """
        definition = tool_to_dict(cls, self.known)
        sources = definition.pop("sources", None)
        if sources:
            self.sources.update(sources)
        return tool_from_dict, (definition,)

    def persistent_id(self, value: Any) -> None:
        if type(value) in PLAIN_TYPES:
            return None
        module = ModuleBundle.declared_module(value)
        if module is not None and module.__name__ not in self.known:
            # The whole bundle of the package comes from the cache. Asking for
            # the missing part instead walks the package again, because that
            # answer depends on the caller and cannot be cached.
            for name, source in ModuleBundle.build(module).items():
                if name not in self.known:
                    self.sources[name] = source
        return None


class FragmentBuffer:
    """Reassembly buffer for packets that arrive in more than one frame.

    Frames of different packets can interleave, so each packet_id keeps its own
    buffer. The limits stop a faulty peer from exhausting memory.
    """

    def __init__(self, max_bytes: int, max_packets: int) -> None:
        self.max_bytes = max_bytes
        self.max_packets = max_packets
        # The frames of a packet are kept as they arrived and joined once. A
        # growing bytearray would copy them while it grows, and the immutable
        # result would copy every byte a second time.
        self.buffers: dict[int, list[bytes]] = {}
        self.total_bytes = 0

    def store(self, packet_id: int, chunk: bytes) -> None:
        """Append one non-final fragment. Raise ValueError when a limit is passed."""
        buffer = self.buffers.get(packet_id)
        if buffer is None:
            if len(self.buffers) >= self.max_packets:
                raise ValueError(f"Too many incomplete packets: {len(self.buffers) + 1}")
            buffer = []
            self.buffers[packet_id] = buffer
        if self.total_bytes + len(chunk) > self.max_bytes:
            raise ValueError(f"Reassembly buffer overflow: {self.total_bytes + len(chunk)} bytes")
        buffer.append(chunk)
        self.total_bytes += len(chunk)

    def take(self, packet_id: int, tail: bytes) -> bytes:
        """Join the stored fragments with the final chunk. Release the buffer."""
        buffer = self.buffers.pop(packet_id, None)
        if buffer is None:
            return tail
        self.total_bytes -= sum(map(len, buffer))
        buffer.append(tail)
        return b"".join(buffer)

    def clear(self) -> None:
        """Drop every buffered fragment."""
        self.buffers.clear()
        self.total_bytes = 0


@dataclass(slots=True)
class Packet:
    """One complete packet that arrived from the peer.

    *size* is the length of the serialized payload before compression. A
    streaming consumer needs it, to give the sender back the budget it spent.
    """

    payload: Any
    flags: "Flags"
    packet_id: int
    size: int


@dataclass(slots=True)
class StreamCredit:
    """Permission to send the items of one streaming response.

    The budget is the serialized size of the items in flight, measured before
    compression. That is not the memory of the Python objects, which can be
    larger or smaller, but it is what the peer has to hold and what the wire
    carries.

    A count limit goes with the budget, so a long run of small items cannot pass
    it. The consumer gives both back as it takes the items.
    """

    max_bytes: int
    max_items: int
    bytes_in_flight: int = 0
    items_in_flight: int = 0
    released: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.ready.set()

    def fits(self, size: int) -> bool:
        """True when one more item of *size* bytes may go out now.

        An item larger than the whole budget goes out alone, once nothing else
        is in flight. It therefore never waits for room that cannot appear.
        """
        if self.items_in_flight >= self.max_items:
            return False
        if self.bytes_in_flight == 0:
            return True
        return self.bytes_in_flight + size <= self.max_bytes

    async def spend(self, size: int) -> bool:
        """Wait for permission for one item of *size* bytes.

        Returns:
            True when the item may go out. False when the sender must stop,
            because the consumer left or the connection is gone.
        """
        while not self.released and not self.fits(size):
            self.ready.clear()
            await self.ready.wait()
        if self.released:
            return False
        self.bytes_in_flight += size
        self.items_in_flight += 1
        return True

    def give_back(self, size: int, items: int) -> None:
        """Take back the budget of items the consumer has taken.

        Raises:
            ValueError: The peer returned more than it holds, or a negative
                amount. Both mean a broken peer, so the stream must not go on.
        """
        if size < 0 or items < 0:
            raise ValueError(f"Negative stream permission: {size} bytes, {items} items")
        if size > self.bytes_in_flight or items > self.items_in_flight:
            raise ValueError(
                f"Stream permission above what is in flight: {size} bytes and {items} items "
                f"against {self.bytes_in_flight} bytes and {self.items_in_flight} items"
            )
        self.bytes_in_flight -= size
        self.items_in_flight -= items
        self.ready.set()

    def release(self) -> None:
        """Stop the sender, because the consumer left or the link is gone."""
        self.released = True
        self.ready.set()


class Flags(enum.IntFlag):
    COMPRESSED = 1
    REQUEST = 1 << 1
    RESPONSE = 1 << 2
    SYNC = 1 << 3
    RPC = 1 << 4
    EXCEPTION = 1 << 5
    LOG = 1 << 6
    FRAGMENT = 1 << 7
    # One item of a streaming response. More items can follow, and the ordinary
    # response that comes after them ends the stream.
    STREAM = 1 << 8
    # An outer source manifest precedes the object pickle.
    MODULES = 1 << 9
    # The payload holds several items of one stream, each with its length.
    BATCH = 1 << 10


class FrameCompressor:
    """Compress frame bodies with one dictionary that serves the direction.

    Every compressed body continues the deflate stream of its direction and
    ends with a sync flush, so a body that repeats the words of an earlier one
    carries a reference instead of the words. The dictionary therefore holds
    the history of the connection, which is what makes a small packet cheap.

    A body that cannot shrink travels raw and leaves the dictionary untouched.
    The density of a short sample decides that, because it costs a microsecond
    where a copy of the dictionary costs ten: the deflate state holds its whole
    window, so a trial on a copy would cost more than the pass it tests. A body
    that passes the sample and still does not shrink therefore travels
    compressed and grows by the few bytes of its deflate block.
    """

    def __init__(self, level: int, sample: int, values: int) -> None:
        self.codec = zlib.compressobj(level)
        self.sample = sample
        self.values = values

    def dense(self, payload: bytes) -> bool:
        """True when a short sample uses too many byte values to shrink.

        This is a cheap guess that keeps random and already compressed bodies
        out of the dictionary. It can refuse a body that would have shrunk a
        little, which costs bytes and no time.
        """
        return len(set(payload[: self.sample])) > self.values

    def encode(self, payload: bytes, *, allowed: bool = True) -> tuple[bytes, bool]:
        """Return the body to write, and whether the dictionary now holds it.

        A caller that passes ``allowed=False`` keeps its body out of the
        dictionary, which is how a tool refuses compression for data it knows
        cannot shrink.
        """
        if not allowed or self.dense(payload):
            return payload, False
        return self.codec.compress(payload) + self.codec.flush(zlib.Z_SYNC_FLUSH), True


class FrameDecompressor:
    """Inflate the marked bodies of one direction, in the order they arrive.

    The dictionary holds every body that was marked, so the bodies must be
    inflated in the order they were written, before the fragments of a packet
    are joined. A raw body passes by and changes nothing.
    """

    # The four bytes that zlib writes for a sync flush. A body without them is
    # not a complete flush, so the dictionary of the sender cannot be followed.
    SYNC_TAIL = b"\x00\x00\xff\xff"

    def __init__(self, limit: int) -> None:
        self.codec = zlib.decompressobj()
        self.limit = limit

    def decode(self, body: bytes) -> bytes:
        """Inflate one marked body.

        Raises:
            ValueError: The body does not end at a flush boundary, inflates
                above the frame limit, or carries bytes after the end of the
                stream. The direction cannot be read after any of them,
                because the dictionary no longer matches the sender.
        """
        if not body.endswith(self.SYNC_TAIL):
            raise ValueError("Compressed frame does not end at a flush boundary")
        payload = self.codec.decompress(body, self.limit + 1)
        if len(payload) > self.limit or self.codec.unconsumed_tail or self.codec.unused_data or self.codec.eof:
            raise ValueError("Compressed frame is broken or above the frame limit")
        return payload


class BaseProtocol:
    # Five-byte magic, uint32 flags and payload length, uint64 packet id.
    MAGIC = b"RMOTE"
    BOUNDARY = b"PROTOCOL READY\n"
    PACKET_HEADER = struct.Struct(">5sIIQ")

    # Length of one item inside a packet that carries a batch of them.
    ITEM_HEADER = struct.Struct(">I")

    # Compression policy. A payload at or below the threshold travels as it is.
    #
    # The level is 3, not the 9 that gzip uses by default. On the source of
    # this package level 9 costs 3.4 times the time of level 6 for 0.7% fewer
    # bytes, and level 3 keeps a ratio above 100 on text while costing a
    # ninth of level 9. No channel notices the difference between a ratio of
    # 100 and one of 340.
    #
    # Random or already compressed data never shrinks, so a sample decides
    # whether the whole payload is worth a pass. A sample that keeps more than
    # COMPRESSION_RATIO of its bytes means the rest will not shrink either.
    #
    # A hand-off to a worker thread costs about 28 microseconds, whatever the
    # work. Below COMPRESSION_INLINE the pass itself is cheaper than that, so
    # it runs on the loop; above it the loop stays free instead.
    # Decompression decides on the compressed size, which understates the
    # work: a small frame with a high ratio expands into a large one. Its
    # limit is therefore well below the one for compression.
    COMPRESSION_THRESHOLD = 1024
    COMPRESSION_LEVEL = 3
    COMPRESSION_SAMPLE = 8 * 1024
    COMPRESSION_RATIO = 0.95
    COMPRESSION_INLINE = 256 * 1024
    DECOMPRESSION_INLINE = 32 * 1024

    # The frame codec compresses the body of each frame with one dictionary
    # per direction, which the sync flush of every compressed body keeps. It
    # replaces the policy that compressed each packet on its own: a small
    # packet shrinks only against the history of the connection. False keeps
    # the packet policy, which the benchmarks compare against.
    FRAME_CODEC: ClassVar[bool] = True

    # A payload that cannot shrink uses most of the byte values, and counting
    # them in a short sample costs about one microsecond where a pass over
    # 1 KiB costs fourteen. Measured on this package: random and already
    # compressed data reach 153 to 155 distinct values of 256, while text,
    # source, JSON and base64 stay between 1 and 62.
    DENSITY_SAMPLE = 256
    DENSITY_LIMIT = 128

    # Maximum payload bytes in one frame. Larger packets are split, so a big
    # transfer cannot hold the channel for its whole duration.
    FRAGMENT_SIZE = 64 * 1024

    # Limits for the receive side. They stop a faulty peer from exhausting memory.
    MAX_REASSEMBLY_BYTES = 256 * 1024 * 1024
    MAX_PARTIAL_PACKETS = 256

    # Budget of one streaming response: the serialized bytes that may be in
    # flight before the consumer gives them back. It bounds the memory a slow
    # consumer can hold.
    MAX_STREAM_WINDOW = 4 * 1024 * 1024

    # Items that may be in flight at the same time. It stops a long run of small
    # items from passing the byte budget.
    MAX_STREAM_BACKLOG = 256

    # Evidence kept from a peer that never reaches the ready boundary. The
    # limits stop a chatty or faulty peer from building a huge message.
    MAX_START_FAILURE_LINES = 10
    MAX_START_FAILURE_TEXT = 2000

    # Largest serialized item of a streaming response. A larger item is refused
    # with a clear error instead of waiting for room that cannot appear.
    MAX_STREAM_ITEM = 64 * 1024 * 1024

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.read_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self._compression: ContextVar[bool] = ContextVar("rpc_compression", default=True)

        # The codecs of the two directions. They are installed at the ready
        # boundary, because the bootstrap and the boundary travel plain.
        self.deflate: FrameCompressor | None = None
        self.inflate: FrameDecompressor | None = None

        self.known_modules: set[str] = set()
        self.fragments = FragmentBuffer(self.MAX_REASSEMBLY_BYTES, self.MAX_PARTIAL_PACKETS)
        # The last lines the transport wrote to its stderr. They are the only
        # evidence of a failure that happens before the peer runs our code.
        self.transport_said: deque[str] = deque(maxlen=self.MAX_START_FAILURE_LINES)
        self._stderr_task: asyncio.Task[None] | None = None

    async def receive(self) -> Packet:
        """Read frames until one logical packet is complete, then return it.

        Frames of different packet_id can interleave. Fragments are buffered per
        packet_id, so a packet that completes first returns first.
        """
        async with self.read_lock:
            while True:
                header = await self.reader.readexactly(self.PACKET_HEADER.size)
                magic, raw_flags, length, packet_id = self.PACKET_HEADER.unpack(header)
                flags = Flags(raw_flags)
                if magic != self.MAGIC:
                    raise ValueError("Invalid magic number")
                limit = self.FRAGMENT_SIZE
                if flags & Flags.COMPRESSED and self.inflate is not None:
                    # A density sample can accept incompressible data. Allow
                    # deflate's block overhead and the sync-flush boundary;
                    # FrameDecompressor still bounds the decoded body.
                    limit += (limit >> 12) + (limit >> 14) + 32
                if length > limit:
                    raise ValueError(f"Frame of {length} bytes is above the limit of {limit}")
                chunk = await self.reader.readexactly(length)
                if flags & Flags.COMPRESSED and self.inflate is not None:
                    # The dictionary follows the wire, so the body is inflated
                    # here and not after the fragments of its packet are joined.
                    chunk = self.inflate.decode(chunk)
                    flags &= ~Flags.COMPRESSED

                if flags & Flags.FRAGMENT:
                    self.fragments.store(packet_id, chunk)
                    continue

                payload = self.fragments.take(packet_id, chunk)
                if flags & Flags.COMPRESSED:
                    payload = await self.unpack(payload)
                size = len(payload)
                if flags & Flags.MODULES:
                    sources, payload = pickle.loads(payload)
                    ModuleLoader.install(sources)
                    self.known_modules.update(sources)
                if flags & Flags.BATCH:
                    return Packet(self.split(payload), flags, packet_id, size)
                return Packet(pickle.loads(payload), flags, packet_id, size)

    async def send(self, packet: Any, flags: Flags, packet_id: int) -> None:
        flags = Flags(flags)
        if flags & Flags.COMPRESSED:
            raise ValueError("Compression flag must not be set for send()")
        if flags & Flags.FRAGMENT:
            raise ValueError("Fragment flag must not be set for send()")
        if flags & Flags.MODULES:
            raise ValueError("Modules flag must not be set for send()")

        # Serialize outside the write lock. Only the frame writes need the lock.
        if flags & Flags.RPC and flags & Flags.REQUEST and not flags & Flags.STREAM:
            if not self._compression.get():
                packet = {**packet, "compressed": False}
        payload, flags, modules = self.serialize(packet, flags)
        await self.send_serialized(payload, flags, packet_id)
        self.known_modules.update(modules)

    def encode(self, value: Any) -> tuple[bytes, dict[str, ModuleSource]]:
        """Pickle one value, and report the sources the peer does not have.

        The caller decides where the sources travel. One value of its own
        carries them in the envelope of its packet; the items of a batch share
        one envelope for all of them.
        """
        stream = io.BytesIO()
        pickler = ModulePickler(stream, self.known_modules)
        pickler.dump(value)
        return stream.getvalue(), {
            name: source for name, source in pickler.sources.items() if name not in self.known_modules
        }

    def serialize(self, value: Any, flags: Flags) -> tuple[bytes, Flags, set[str]]:
        """Put source definitions outside the object pickle, before it is decoded."""
        payload, sources = self.encode(value)
        if sources:
            payload = pickle.dumps((sources, payload))
            flags |= Flags.MODULES
        return payload, flags, set(sources)

    @classmethod
    def batch(cls, frames: list[bytes]) -> bytes:
        """Join pickled items into one payload, each behind its length."""
        parts: list[bytes] = []
        for frame in frames:
            parts.append(cls.ITEM_HEADER.pack(len(frame)))
            parts.append(frame)
        return b"".join(parts)

    @classmethod
    def split(cls, payload: bytes) -> list[Any]:
        """Decode the items of one batch payload, in the order they were sent.

        Raises:
            ValueError: The lengths do not describe the payload, which means a
                broken peer. The connection cannot continue after that.
        """
        view = memoryview(payload)
        items: list[Any] = []
        position = 0
        while position < len(view):
            if position + cls.ITEM_HEADER.size > len(view):
                raise ValueError("Truncated item header in a stream batch")
            (length,) = cls.ITEM_HEADER.unpack_from(view, position)
            position += cls.ITEM_HEADER.size
            if position + length > len(view):
                raise ValueError("Truncated item in a stream batch")
            items.append(pickle.loads(view[position : position + length]))
            position += length
        return items

    def worth_compressing(self, payload: bytes) -> bool:
        """Report whether a sample of *payload* shrinks enough to compress it all.

        A sample of the head decides for the whole payload. Random and already
        compressed data keep their size, and a pass over them only spends
        processor time.
        """
        sample = payload[: self.COMPRESSION_SAMPLE]
        packed = zlib.compress(sample, self.COMPRESSION_LEVEL)
        return len(packed) < len(sample) * self.COMPRESSION_RATIO

    def dense(self, payload: bytes) -> bool:
        """True when a short sample uses too many byte values to shrink.

        This is a guess, and a cheap one. It can refuse a payload that would
        have shrunk a little, which costs bytes but no time. It cannot make a
        packet grow, because the result of every pass is checked as well.
        """
        return len(set(payload[: self.DENSITY_SAMPLE])) > self.DENSITY_LIMIT

    def compressed(self, payload: bytes) -> bytes | None:
        """Compress *payload* when that pays. None keeps the original bytes.

        Three tests, from the cheapest to the most expensive: the byte values
        of a short sample, a compression pass over a larger sample, and the
        result of the full pass. The last one is unconditional, so a packet
        never grows on the wire.
        """
        if self.dense(payload):
            return None
        if len(payload) > self.COMPRESSION_SAMPLE and not self.worth_compressing(payload):
            return None
        packed = compress(payload, self.COMPRESSION_LEVEL)
        return packed if len(packed) < len(payload) else None

    async def pack(self, payload: bytes) -> bytes | None:
        """Compress off the loop only when the work pays for the hand-off.

        The density check costs about a microsecond, so it runs here: a large
        payload that cannot shrink is refused without paying for a worker
        thread at all.
        """
        if self.dense(payload):
            return None
        if len(payload) <= self.COMPRESSION_INLINE:
            return self.compressed(payload)
        return await asyncio.to_thread(self.compressed, payload)

    async def unpack(self, payload: bytes) -> bytes:
        """Decompress, off the loop only when the work pays for the hand-off.

        The decision uses the compressed size, so the limit stays low: the
        work is proportional to the result, which a high ratio makes much
        larger than the frame that carries it.
        """
        if len(payload) <= self.DECOMPRESSION_INLINE:
            return decompress(payload)
        return cast(bytes, await asyncio.to_thread(decompress, payload))

    async def send_serialized(self, payload: bytes, flags: Flags, packet_id: int) -> None:
        """Compress a serialized payload when that pays, then write it.

        A streaming sender serializes the item itself, because it needs the size
        for its budget. This method takes that work and does not repeat it.

        The frame codec compresses the body of each frame instead, so this
        method does nothing while that codec is installed.
        """
        if not self.FRAME_CODEC and self._compression.get() and len(payload) > self.COMPRESSION_THRESHOLD:
            packed = await self.pack(payload)
            if packed is not None:
                payload, flags = packed, flags | Flags.COMPRESSED
        await self.send_payload(payload, flags, packet_id)

    async def send_payload(self, payload: bytes, flags: Flags, packet_id: int) -> None:
        """Write one logical packet. Split it into fragments when it is large.

        Every fragment carries the logical flags. The FRAGMENT flag marks all
        fragments except the last. The write lock is released between fragments,
        so packets with a different packet_id interleave instead of waiting.

        One packet_id must be sent by one task only. The peer joins its fragments
        in arrival order, and concurrent senders would mix their bytes.
        """
        total = len(payload)
        offset = 0
        while True:
            chunk = payload[offset : offset + self.FRAGMENT_SIZE]
            offset += len(chunk)
            if offset >= total:
                await self.send_frame(chunk, flags, packet_id)
                return
            await self.send_frame(chunk, flags | Flags.FRAGMENT, packet_id)

    async def send_frame(self, payload: bytes, flags: Flags, packet_id: int) -> None:
        """Write one frame as a single unit. Hold the write lock for that frame.

        The frame codec compresses the body here, because the dictionary of
        the direction must move in the order the bodies reach the wire. The
        work stays on the loop: the lock serializes it anyway, so a worker
        thread would only add its hand-off to the price.
        """
        async with self.write_lock:
            if self.writer.is_closing():
                raise ConnectionError("Connection closed")
            try:
                if self.deflate is not None:
                    payload, packed = self.deflate.encode(payload, allowed=self._compression.get())
                    if packed:
                        flags |= Flags.COMPRESSED
                header = self.PACKET_HEADER.pack(self.MAGIC, flags, len(payload), packet_id)
                self.writer.write(header + payload)
                await self.writer.drain()
            except BaseException:
                # The peer may have received only part of the packet, and a
                # dictionary that moved cannot be moved back.
                self.writer.close()
                raise

    async def write_boundary(self) -> None:
        """Write the ready boundary, then compress what follows it."""
        async with self.write_lock:
            self.writer.write(self.BOUNDARY)
            await self.writer.drain()
            if self.FRAME_CODEC:
                self.deflate = FrameCompressor(self.COMPRESSION_LEVEL, self.DENSITY_SAMPLE, self.DENSITY_LIMIT)

    @classmethod
    def start_failure(cls, said: Iterable[str], transport: Iterable[str] = ()) -> str:
        """Build the reason a peer did not reach the ready boundary.

        Two streams carry evidence. The protocol stream holds what the peer
        said before the boundary. The stderr of the transport holds what the
        interpreter or the transport itself said, and that is the only evidence
        of a failure before our code runs: a missing module, a syntax error of
        an old interpreter, or ssh that refuses to connect. The tail of each
        one is kept, because the cause of a traceback is its last line.
        """
        reason = "Remote process closed the connection before PROTOCOL READY"
        parts = [reason]
        text = " | ".join(said)
        if text:
            parts.append(f"It said: {text[-cls.MAX_START_FAILURE_TEXT :]}")
        noise = " | ".join(transport)
        if noise:
            parts.append(f"Its transport said: {noise[-cls.MAX_START_FAILURE_TEXT :]}")
        return ". ".join(parts)

    def watch_stderr(self, reader: asyncio.StreamReader) -> None:
        """Read the stderr of the transport for as long as it lasts.

        The lines of the start go into the failure of the handshake. The
        reading continues after the start, because a transport that writes
        more would otherwise fill the pipe and block on its own write. Only
        the last lines are kept, so a chatty host costs no memory.
        """
        self._stderr_task = asyncio.create_task(self.collect_stderr(reader))

    async def collect_stderr(self, reader: asyncio.StreamReader) -> None:
        """Keep the last lines of *reader* until it ends."""
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # One line above the limit of the reader. It is already
                # consumed, so the next one is read.
                continue
            if not line:
                return
            spoken = line.decode("utf-8", "replace").strip()
            if spoken:
                self.transport_said.append(spoken)

    @staticmethod
    def report_start_failure(stream: int, report: int, error: BaseException) -> None:
        """Report why this side cannot start, on both channels it still has.

        One plain line goes to the protocol stream, before the ready boundary,
        so the peer reports a cause instead of an unexplained end of
        connection. The complete traceback goes to the original stderr, for
        whoever reads the transport. Raw descriptors are used, because the
        failure can be the channel setup itself.

        This writes to the protocol stream only while the connection is being
        abandoned, so it cannot corrupt a packet. A failed report is ignored:
        the original failure is what matters.
        """
        reason = f"rmote remote failed to start: {type(error).__name__}: {error}\n"
        try:
            os.write(stream, reason.encode("utf-8", "replace"))
        except OSError:
            pass
        try:
            os.write(report, reason.encode("utf-8", "replace"))
            os.write(report, "".join(traceback.format_exception(error)).encode("utf-8", "replace"))
        except OSError:
            pass

    async def read_boundary(self) -> None:
        """Wait for the ready boundary, keeping what the peer said before it.

        A peer that cannot start writes its reason to this stream, and its
        interpreter can write a traceback here too. Those lines go into the
        error, so the caller learns the cause instead of only the symptom.

        The boundary also opens the codec of this direction: everything before
        it travels plain, and every frame after it can carry a compressed body.
        """
        said: deque[str] = deque(maxlen=self.MAX_START_FAILURE_LINES)
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
                    if self.FRAME_CODEC:
                        self.inflate = FrameDecompressor(self.FRAGMENT_SIZE)
                    return
                if not line:
                    raise ConnectionError(self.start_failure(said, self.transport_said))
                spoken = line.decode("utf-8", "replace").strip()
                if spoken:
                    said.append(spoken)

    @classmethod
    async def from_subprocess(cls, process: asyncio.subprocess.Process) -> Self:
        assert process.stdin is not None, "Process stdin must not be None"
        assert process.stdout is not None, "Process stdout must not be None"
        process.stdin.write(bootstrap_payload())
        process.stdin.write(b"asyncio.run(_protocol.run())\n")
        instance = cls(reader=process.stdout, writer=process.stdin)
        if process.stderr is not None:
            # The transport keeps its own stderr only when the caller asked
            # for a pipe. Then this side reads it, for the failure of the
            # start and to keep the pipe empty afterwards.
            instance.watch_stderr(process.stderr)
        return instance

    @classmethod
    async def from_command(
        cls,
        *argv: str,
        python: str = "python3",
        stderr: int = asyncio.subprocess.PIPE,
        env: dict[str, str] | None = None,
    ) -> Self:
        """Bootstrap a Protocol over any command that starts a remote Python REPL.

        The interpreter and its flags are appended to *argv*. For example,
        ``from_command("ssh", "-T", "host")`` runs ``ssh -T host python3 -qui``.
        An empty *argv* starts the interpreter on the local host.

        Args:
            *argv: Transport command and its arguments. It must pass stdin and
                stdout through without changing the bytes.
            python: Python executable to start at the far end.
            stderr: Where to redirect the remote stderr. The default pipe
                is read by this side: its last lines explain a failed
                start, and the rest is dropped. Pass DEVNULL to discard
                it, or a descriptor to let the caller see it live.
            env: Environment for the transport command. None inherits it.

        Returns:
            A connected instance. The caller must still enter its context.
        """
        proc = await asyncio.create_subprocess_exec(
            *argv,
            python,
            "-qui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env=env,
        )
        instance = await cls.from_subprocess(proc)
        instance._owned_process = proc  # type: ignore[attr-defined]
        return instance

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
        stderr: int = asyncio.subprocess.PIPE,
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
            stderr: Where to redirect remote stderr. The default pipe is
                read by this side, so a refusal of ssh appears in the
                error of the start.
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
        cmd += [host]

        return await cls.from_command(*cmd, python=python, stderr=stderr)

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
        # Keep the original stderr. The redirection below hides every failure
        # of this method, and this descriptor is the only way to report one to
        # whoever reads the transport.
        report_fd = os.dup(sys.stderr.fileno())
        os.set_inheritable(proto_in_fd, False)
        os.set_inheritable(proto_out_fd, False)
        os.set_inheritable(report_fd, False)

        # Redirect before anything else can write. One byte on descriptor 1
        # corrupts the packet stream for the whole connection, so the
        # protection must not wait for the channel setup below.
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

        try:
            reader, writer = await wrap_reader(), await wrap_writer()
        except BaseException as error:
            cls.report_start_failure(proto_out_fd, report_fd, error)
            raise
        finally:
            os.close(report_fd)

        protocol = cls(reader, writer)
        log_handler = RemoteLogHandler(protocol, loop)  # type: ignore[arg-type]
        protocol.log_handler = log_handler  # type: ignore[attr-defined]
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(log_handler)
        root_logger.setLevel(logging_level)
        return protocol


class StreamBuffer:
    """Collect the items of one streaming response, and write them together.

    The producer pickles an item, pays for it, and leaves it here. This writer
    sends what gathered as one packet. A producer that does not wait between
    items fills the buffer while a packet is on the wire, so one write and one
    read serve a burst instead of serving every item. A producer that waits
    lets the writer run at once, so the latency of one item does not change.

    The budget of the stream bounds the buffer, because the producer pays for
    an item before it arrives here and the consumer pays it back as it takes
    the items.
    """

    def __init__(self, protocol: "Protocol", packet_id: int) -> None:
        self.protocol = protocol
        self.packet_id = packet_id
        self.frames: list[bytes] = []
        self.sources: dict[str, ModuleSource] = {}
        self.ready = asyncio.Event()
        self.done = False

    def push(self, payload: bytes, sources: dict[str, ModuleSource]) -> None:
        """Hand one paid item to the writer."""
        self.frames.append(payload)
        self.sources.update(sources)
        self.ready.set()

    def finish(self) -> None:
        """Report that no more items will arrive."""
        self.done = True
        self.ready.set()

    async def run(self) -> None:
        """Write what gathered, until the producer says there is no more."""
        while True:
            await self.ready.wait()
            self.ready.clear()
            frames, self.frames = self.frames, []
            sources, self.sources = self.sources, {}
            if frames:
                await self.write(frames, sources)
            if self.done and not self.frames:
                return

    async def write(self, frames: list[bytes], sources: dict[str, ModuleSource]) -> None:
        """Send one packet with every item that gathered.

        The items share one source manifest, because they travel in one packet.
        A large packet is split into frames by the transport, so a batch does
        not hold the channel against the other calls of the connection.
        """
        payload = self.protocol.batch(frames)
        flags = Flags.RPC | Flags.RESPONSE | Flags.STREAM | Flags.BATCH
        if sources:
            payload = pickle.dumps((sources, payload))
            flags |= Flags.MODULES
        await self.protocol.send_serialized(payload, flags, self.packet_id)
        self.protocol.known_modules.update(sources)


P = ParamSpec("P")
R = TypeVar("R")


class Protocol(BaseProtocol):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        super().__init__(reader, writer)
        # The key the peer registered every tool under, by class or module.
        self._tools_cache: dict[type[Tool] | ModuleType, str] = {}
        # Keyed by the name of a bundle root, or by a class that has no bundle.
        self._tool_sync_locks: dict[Any, asyncio.Lock] = {}
        self._tool_load_lock = asyncio.Lock()
        self.futures: dict[int, asyncio.Future[Any]] = {}
        # Queues of the streaming calls that are open, keyed by packet_id.
        self.streams: dict[int, asyncio.Queue[tuple[Flags, Any, int]]] = {}
        # Serialized bytes that arrived and still wait for their consumer.
        self.stream_buffered: dict[int, int] = {}
        # Streams whose backlog filled up. Their failure is already reported.
        self.streams_over_budget: set[int] = set()
        # Permission to send, for every streaming response this side produces.
        self.stream_credits: dict[int, StreamCredit] = {}
        # The task that produces every streaming response this side sends. A
        # consumer that leaves cancels it, so a producer that waits for data
        # stops as well.
        self.stream_senders: dict[int, asyncio.Task[Any]] = {}
        self.loop = asyncio.get_running_loop()
        self._loop_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._close_error: Exception | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.tools: dict[str, Tool | ModuleType] = dict()
        self._logs = LogDelivery()
        # The handler of this side, set when this process is the remote one.
        # Its records travel with the next response instead of a packet of
        # their own.
        self.log_handler: RemoteLogHandler | None = None
        # Requests that are being served and still owe a response. A record
        # that appears now has a packet to travel in.
        self.responses_pending = 0

        self._owned_process: asyncio.subprocess.Process | None = None

        # Sync ID generation for RPC/SYNC packets
        self.__last_id = 0
        self.__last_id_lock = threading.Lock()

        # LOG IDs count down within the unsigned 64-bit wire field; RPC IDs count up.
        self.__last_log_id = 1 << 64
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
        try:
            await self.write_boundary()
            await self.read_boundary()
        except BaseException:
            # A failed __aenter__ is not followed by __aexit__ by Python.
            await self.__aexit__(*sys.exc_info())
            raise
        self._loop_task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._finish_pending(ConnectionError("Connection closed"))
        tasks = list(self._tasks)
        if self._loop_task:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
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
            # Process.wait() can return as soon as returncode is set, even
            # with paused or inherited output pipes still open. asyncio has
            # no public Process.close(); the owned transport closes all pipes.
            self._owned_process._transport.close()  # type: ignore[attr-defined]
            await self._owned_process.wait()
            self._owned_process = None
        # Records that arrived before the close must reach their handlers.
        await asyncio.to_thread(self._logs.stop)

    async def _load_tool(self, tool_definition: dict[str, Any], _: int) -> None:
        key = tool_key(tool_definition)
        # One lock for every load. Modules of one package import each other,
        # and two threads that import siblings deadlock in importlib.
        async with self._tool_load_lock:
            if key in self.tools:
                return

            def build_tool() -> Tool | ModuleType:
                if tool_definition.get("kind") == "module":
                    return ModuleLoader.load(tool_definition["module"], tool_definition["sources"])
                return tool_from_dict(tool_definition)()

            instance = await asyncio.to_thread(build_tool)
            self.tools[key] = instance
            self.known_modules.update(tool_definition.get("dependencies", {}))
            self.known_modules.update(tool_definition.get("sources", {}))
            logging.debug("Loaded tool %s", tool_definition)

    async def _handle_rpc_request(self, request: RPCRequest, packet_id: int) -> Any:
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

        if inspect.isasyncgenfunction(method):
            # The items become responses of their own, so the peer does not
            # send a request per item. The ordinary response of this handler
            # follows the items and ends the stream.
            await self.send_stream(method(*request["args"], **request["kwargs"]), packet_id)
            return None

        if inspect.iscoroutinefunction(method):
            return await method(*request["args"], **request["kwargs"])

        if runs_on_loop(method):
            # The author promised the method does not wait, so it is cheaper
            # to call it here than to wake a worker thread for it.
            return method(*request["args"], **request["kwargs"])

        return await asyncio.to_thread(method, *request["args"], **request["kwargs"])

    async def send_stream(self, items: AsyncIterator[Any], packet_id: int) -> None:
        """Send the items of a streaming method, several of them in one packet.

        A separate task writes what gathered, so the items that the producer
        makes while a packet is on the wire travel in the next one. Each item
        needs permission from the consumer before it reaches that task, so a
        consumer that reads slowly slows the producer and one that leaves stops
        it. The permission is awaited before the write lock is taken, so a
        waiting stream never holds the transport.

        Raises:
            ValueError: One item is above MAX_STREAM_ITEM. The consumer gets the
                failure, because an item that large could never find room.
        """
        credit = StreamCredit(self.MAX_STREAM_WINDOW, self.MAX_STREAM_BACKLOG)
        self.stream_credits[packet_id] = credit
        producer = asyncio.current_task()
        if producer is not None:
            self.stream_senders[packet_id] = producer
        buffer = StreamBuffer(self, packet_id)
        writer = asyncio.create_task(buffer.run())
        self._tasks.add(writer)
        writer.add_done_callback(self._tasks.discard)
        try:
            async for item in items:
                payload, sources = self.encode(item)
                if len(payload) > self.MAX_STREAM_ITEM:
                    raise ValueError(
                        f"Streaming item of {len(payload)} bytes is above the limit of {self.MAX_STREAM_ITEM} bytes"
                    )
                # The length that goes before the item on the wire is part of
                # what the consumer gives back, so it is part of the price.
                if not await credit.spend(len(payload) + self.ITEM_HEADER.size):
                    # The consumer left. To produce more would waste the host.
                    break
                buffer.push(payload, sources)
        finally:
            self.stream_credits.pop(packet_id, None)
            self.stream_senders.pop(packet_id, None)
            buffer.finish()
            try:
                # What gathered must reach the wire before the response that
                # ends the stream, and a failed write must be reported. To
                # wait for a task does not cancel it, so a cancelled producer
                # leaves the writer to finish: the buffer is closed above, and
                # the writer stops when it is empty. A packet is therefore
                # never half written.
                await writer
            finally:
                # Let the method run its own cleanup. A cancelled producer has
                # already closed its generator through the cancellation.
                closer = getattr(items, "aclose", None)
                if closer is not None:
                    await closer()

    def handle_stream_credit(self, returned: Any, packet_id: int) -> None:
        """Take the permission that a streaming consumer gave back.

        A frame with STREAM and REQUEST always belongs to a stream this side
        produces, and one with STREAM and RESPONSE to a stream this side
        consumes. The direction tells the two registries apart, so the same
        number on both sides is never confused.

        None means the consumer left, so the sender stops.
        """
        credit = self.stream_credits.get(packet_id)
        if credit is None:
            logging.debug("Stream permission for packet %r has no sender", packet_id)
            return
        if returned is None:
            credit.release()
            # A producer that waits for data never reaches its next permission
            # check, so the release alone would leave it waiting until the
            # connection closes. Cancelling its task delivers the cancellation
            # into the generator, which runs the cleanup of the method.
            producer = self.stream_senders.get(packet_id)
            if producer is not None and not producer.done():
                producer.cancel()
            return
        try:
            size, items = returned
            credit.give_back(int(size), int(items))
        except (TypeError, ValueError) as error:
            # A broken peer must not keep the sender running on a false budget.
            logging.error("Bad stream permission for packet %r: %s", packet_id, error)
            credit.release()

    def handle_stream_item(self, flags: Flags, packet_id: int, payload: Any, size: int) -> None:
        """Hand one item of a streaming response to its consumer.

        This stays synchronous, because it runs in the receive loop. The budget
        that the consumer gives the sender already bounds the backlog, so the
        limits here only catch a peer that ignores the budget.
        """
        queue = self.streams.get(packet_id)
        if queue is None:
            # The consumer left the loop. Items of an abandoned stream are
            # dropped, because only its own call can stop the remote method.
            logging.debug("Stream item of packet %r has no consumer", packet_id)
            return

        if packet_id in self.streams_over_budget:
            return

        buffered = self.stream_buffered.get(packet_id, 0) + size
        fits = buffered <= self.MAX_STREAM_WINDOW or queue.empty()
        if queue.qsize() >= self.MAX_STREAM_BACKLOG or size > self.MAX_STREAM_ITEM or not fits:
            # The queue keeps one more slot, so this report always fits. The
            # registration stays, so the end of the stream is absorbed quietly.
            self.streams_over_budget.add(packet_id)
            error = RuntimeError(
                f"Streaming call {packet_id} ignored its permission: "
                f"{queue.qsize()} items and {buffered} bytes are waiting"
            )
            queue.put_nowait((Flags.EXCEPTION | Flags.RESPONSE, error, 0))
            return

        self.stream_buffered[packet_id] = buffered
        queue.put_nowait((flags, payload, size))

    async def _handle_rpc_response(self, response: Any, packet_id: int) -> None:
        queue = self.streams.get(packet_id)
        if queue is not None:
            # The ordinary response after the items ends the stream. A full
            # queue already holds a failure, so the end is not needed.
            if not queue.full():
                queue.put_nowait((Flags.RPC | Flags.RESPONSE, response, 0))
            return
        if packet_id not in self.futures:
            logging.warning("RPC response %r packet not found in futures", packet_id)
            return
        future = self.futures.pop(packet_id)
        if not future.done():
            future.set_result(response)

    async def _handle_exception(self, exception: Exception, packet_id: int) -> None:
        queue = self.streams.get(packet_id)
        if queue is not None:
            if not queue.full():
                queue.put_nowait((Flags.EXCEPTION | Flags.RESPONSE, exception, 0))
            return
        if packet_id not in self.futures:
            logging.warning("Exception response %r packet not found in futures: %s", packet_id, exception)
            return
        future = self.futures.pop(packet_id)
        if not future.done():
            future.set_exception(exception)

    def _execute(
        self,
        packet_id: int,
        flags: Flags,
        handler: Callable[[Any, int], Awaitable[Any]],
        payload: Any,
        need_response: bool = False,
    ) -> None:

        async def wrapper() -> None:
            compressed = payload.get("compressed", True) if handler == self._handle_rpc_request else True
            token = self._compression.set(compressed)
            try:
                try:
                    resp = await handler(payload, packet_id)
                    response_flags = flags | Flags.RESPONSE
                except Exception as e:
                    resp = e
                    response_flags = Flags.EXCEPTION | Flags.RESPONSE

                if need_response:
                    await self._send_response(resp, response_flags, packet_id)
            finally:
                self._compression.reset(token)
                self.responses_pending -= owed

        # Counted before the task starts, so a record of this call always finds
        # the response it can travel with.
        owed = int(need_response)
        self.responses_pending += owed
        task = asyncio.create_task(wrapper())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_response(self, response: Any, flags: Flags, packet_id: int) -> None:
        """Send a response or a portable serialization error; close after a transport failure.

        The log records that gathered while the call ran travel inside this
        packet, which leaves anyway. A record then costs no packet of its own.
        The LOG flag marks the pair, and a response without the flag carries
        the result alone.
        """
        handler = self.log_handler
        batch: list[tuple[logging.LogRecord, LogRecord]] = [] if handler is None else handler.detach()
        if batch:
            flags |= Flags.LOG
            response = ([item[1] for item in batch], response)
        try:
            await self.send(response, flags, packet_id)
        except Exception as error:
            if handler is not None and batch:
                # The records never reached the wire, so they keep their place.
                handler.restore(batch)
            if self.writer.is_closing():
                self._finish_pending(error)
                return
            # Serialization/compression failed before any bytes were written.
            # Error text can itself be unsafe to format or pickle.
            fallback = RuntimeError(f"Remote response serialization failed ({type(error).__name__})")
            try:
                await self.send(fallback, Flags.EXCEPTION | Flags.RESPONSE, packet_id)
            except Exception as fallback_error:
                self.writer.close()
                self._finish_pending(fallback_error)
        else:
            if handler is not None and batch:
                handler.sent()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def _finish_pending(self, error: Exception) -> None:
        if self._close_error is None:
            self._close_error = error
        self._closed.set()
        for future in self.futures.values():
            if not future.done():
                future.set_exception(self._close_error)
        self.futures.clear()
        # A streaming consumer waits on a queue and has no future of its own.
        for stream in self.streams.values():
            if not stream.full():
                stream.put_nowait((Flags.EXCEPTION | Flags.RESPONSE, self._close_error, 0))
        self.streams.clear()
        self.streams_over_budget.clear()
        self.stream_buffered.clear()
        # Senders that wait for permission must not wait for a dead link. The
        # tasks themselves are cancelled by the close of the connection.
        for credit in self.stream_credits.values():
            credit.release()
        self.stream_credits.clear()
        self.stream_senders.clear()

    async def _loop(self) -> None:
        error: Exception | None = None
        try:
            while not self._closed.is_set():
                packet = await self.receive()
                payload, flags, packet_id = packet.payload, packet.flags, packet.packet_id
                logging.debug("Received packet %d with flags %r: %r", packet_id, flags, payload)

                if flags & Flags.LOG and flags & Flags.RESPONSE:
                    # Only an ordinary response carries records. They are
                    # delivered before the response, so they keep the order of
                    # the records that travel in a packet of their own.
                    records, payload = payload
                    self._logs.deliver(records)
                    flags &= ~Flags.LOG

                if flags & Flags.STREAM and flags & Flags.REQUEST:
                    self.handle_stream_credit(payload, packet_id)
                    continue

                if flags & Flags.STREAM and flags & Flags.RESPONSE:
                    self.handle_stream_item(flags, packet_id, payload, packet.size)
                    continue

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
                    # Delivery costs a queue put here. The handlers run in the
                    # worker thread of LogDelivery.
                    self._logs.deliver(payload)
        except Exception as e:
            error = e
        finally:
            self._finish_pending(error or ConnectionError("Remote process closed the connection"))

    async def _call(self, payload: Any, flags: Flags) -> Any:
        if self._closed.is_set():
            raise self._close_error or ConnectionError("Connection closed")
        logging.debug("Call %r %s", flags, payload)
        packet_id = self.get_id()
        future = self.loop.create_future()
        self.futures[packet_id] = future
        try:
            await self.send(payload, flags, packet_id)
            return await future
        except BaseException as error:
            if self.writer.is_closing():
                self._finish_pending(
                    error if isinstance(error, Exception) else ConnectionError("Packet send interrupted")
                )
            raise
        finally:
            self.futures.pop(packet_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A receive/close error can arrive while send is still pending.
                future.exception()

    # A plain return type can also be an async iterator, so the first and the
    # last overload overlap. The call tells them apart at run time.
    #
    # The result is an async generator, not only an iterator, so the caller can
    # close it at once with aclose or contextlib.aclosing.
    @overload
    def __call__(  # type: ignore[overload-overlap]
        self, tool: Callable[P, AsyncIterator[R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncGenerator[R, None]: ...

    @overload
    def __call__(
        self, tool: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...

    @overload
    def __call__(
        self, tool: Callable[P, R | Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...

    def __call__(self, tool: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call a tool method on the peer.

        An ordinary method gives a coroutine, so the caller awaits it. A method
        written as an async generator gives an async generator instead, so the
        caller reads it with ``async for`` and the items arrive as the remote
        side produces them. A bare ``break`` does not close that generator at
        once, so use ``contextlib.aclosing`` when the close has to be immediate.

        A close stops the remote method at its next permission check, which
        happens when the method produces its next item. A method that waits for
        an item it never receives keeps running, so give such a method a
        separate operation that releases its resource.
        """
        if streaming_method(tool):
            return self.stream(tool, *args, **kwargs)
        return self._call_tool(tool, *args, **kwargs)

    @overload
    def uncompressed(  # type: ignore[overload-overlap]
        self, tool: Callable[P, AsyncIterator[R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncGenerator[R, None]: ...

    @overload
    def uncompressed(
        self, tool: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...

    @overload
    def uncompressed(
        self, tool: Callable[P, R | Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...

    def uncompressed(self, tool: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call without compressing the request, response or stream items.

        All arguments belong to the tool, including a keyword named compressed.
        Concurrent calls keep their own compression policy. Stream consumers
        must close the returned generator just as for an ordinary stream call.
        """

        async def call() -> Any:
            token = self._compression.set(False)
            try:
                return await self._call_tool(tool, *args, **kwargs)
            finally:
                self._compression.reset(token)

        async def stream() -> AsyncGenerator[Any, None]:
            source = self.stream(tool, *args, **kwargs)
            try:
                while True:
                    token = self._compression.set(False)
                    try:
                        item = await anext(source)
                    except StopAsyncIteration:
                        return
                    finally:
                        self._compression.reset(token)
                    # Never leave policy installed in the consumer's context.
                    yield item
            finally:
                token = self._compression.set(False)
                try:
                    await source.aclose()
                finally:
                    self._compression.reset(token)

        return stream() if streaming_method(tool) else call()

    def _sync_lock(self, target: "type[Tool] | ModuleType") -> asyncio.Lock:
        """Return the lock that serializes the sync of one module bundle.

        Every class of a package carries the same bundle. Two of them syncing
        at once would each send it in full, because neither sees the modules
        the other is about to announce. An inline class carries its own source
        and shares nothing, so it waits for itself only.
        """
        if isinstance(target, ModuleType):
            module: ModuleType | None = target
        elif "<locals>" in target.__qualname__:
            return self._tool_sync_locks.setdefault(target, asyncio.Lock())
        else:
            module = inspect.getmodule(target)
        if module is None or module.__name__ == "__main__":
            return self._tool_sync_locks.setdefault(target, asyncio.Lock())
        return self._tool_sync_locks.setdefault(ModuleBundle.root(module).__name__, asyncio.Lock())

    async def prepare_call(self, tool: Callable[..., Any]) -> str:
        """Make sure the tool is on the peer. Return the method id to call.

        Raises:
            ValueError: *tool* is neither a Tool method nor an importable module function.
        """
        tool_class = getattr(tool, "__tool_class__", None)
        # For classmethods/staticmethods, check __func__ if __tool_class__ not found on the method itself
        if tool_class is None and hasattr(tool, "__func__"):
            tool_class = getattr(tool.__func__, "__tool_class__", None)
        if tool_class is None:
            module = inspect.getmodule(tool)
            if (
                not isinstance(tool, FunctionType)
                or tool.__qualname__ != tool.__name__
                or module is None
                or module.__name__ == "__main__"
                or getattr(module, tool.__name__, None) is not tool
            ):
                raise ValueError("Only methods of Tool classes can be called, or importable module-level functions")
            if module not in self._tools_cache:
                async with self._sync_lock(module):
                    if module not in self._tools_cache:
                        bundle = ModuleBundle.build(module)
                        definition = {
                            "kind": "module",
                            "name": module.__name__,
                            "module": module.__name__,
                            "sources": {
                                item: source for item, source in bundle.items() if item not in self.known_modules
                            },
                        }
                        await self._call(definition, Flags.SYNC | Flags.REQUEST)
                        self.known_modules.update(definition["sources"])
                        self._tools_cache[module] = tool_key(definition)
            return f"{self._tools_cache[module]}.{tool.__name__}"
        if tool_class not in self._tools_cache:
            async with self._sync_lock(tool_class):
                if tool_class not in self._tools_cache:
                    definition = tool_to_dict(tool_class, self.known_modules)
                    await self._call(definition, Flags.SYNC | Flags.REQUEST)
                    self.known_modules.update(definition.get("dependencies", {}))
                    self.known_modules.update(definition.get("sources", {}))
                    self._tools_cache[tool_class] = tool_key(definition)
        # The key comes from the definition that was sent, so it is the key the
        # peer used. A key built from the class again would have to repeat the
        # test that chose the way, and the two could disagree.
        return f"{self._tools_cache[tool_class]}.{tool.__name__}"

    async def _call_tool(self, tool: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        method_id = await self.prepare_call(tool)
        result: Any = await self._call(
            RPCRequest(method=method_id, args=args, kwargs=kwargs), Flags.RPC | Flags.REQUEST
        )
        return result

    async def stream(
        self, tool: Callable[P, AsyncIterator[R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncGenerator[R, None]:
        """Call a streaming tool method and yield its items as they arrive.

        The method must be an async generator on the remote side. The items
        travel as responses, so the peer pushes without a request per item, and
        the items it has ready travel together in one packet. A large packet is
        fragmented, so a long stream does not hold the channel and ordinary
        calls keep their progress.

        Closing the iterator tells the sender to stop. The task that produces
        the items is cancelled, so a generator that waits for data it never
        receives is interrupted as well, and the cleanup of the method runs in
        either case. Use contextlib.aclosing for a deterministic close on an
        early break.

        Synchronous code inside the method cannot be interrupted: the
        cancellation arrives at the next await. A method that holds a resource
        between calls still needs its own operation to release it, exactly as
        an ordinary method does.

        The items that the sender has ready travel in one packet, so a write
        on one side and a read on the other serve a burst instead of serving
        every item. One connection therefore carries many streams at the same
        price: with 64-byte items the cost is near 2 µs per item from one
        stream to thirty-two. An item that is alone travels at once, so a
        producer that waits between items keeps the latency of each one.

        Equal streams do not finish together: the last of thirty-two finishes
        about 40 percent of the run after the first, because a stream that is
        served sends a whole batch. Where every stream must advance evenly,
        give each one its own connection.

        Args:
            tool: Method of a Tool class, defined as an async generator.
            *args: Positional arguments of the method.
            **kwargs: Keyword arguments of the method.

        Yields:
            Items in the order the remote side produced them.

        Raises:
            ConnectionError: The connection dropped while the stream was open.
            RuntimeError: The peer exceeded the stream budget or item limit.
            Exception: Whatever the remote method raised.
        """
        method_id = await self.prepare_call(tool)
        packet_id = self.get_id()
        # One slot above the backlog stays free for the end or the failure.
        queue: asyncio.Queue[tuple[Flags, Any, int]] = asyncio.Queue(maxsize=self.MAX_STREAM_BACKLOG + 1)
        self.streams[packet_id] = queue
        self.stream_buffered[packet_id] = 0
        finished = False
        owed_bytes = 0
        owed_items = 0
        try:
            await self.send(
                RPCRequest(method=method_id, args=args, kwargs=kwargs), Flags.RPC | Flags.REQUEST, packet_id
            )
            while True:
                flags, payload, size = await queue.get()
                self.stream_buffered[packet_id] = max(0, self.stream_buffered.get(packet_id, 0) - size)

                if flags & Flags.EXCEPTION:
                    finished = True
                    raise payload
                if not flags & Flags.STREAM:
                    # The ordinary response ends the stream.
                    finished = True
                    return
                if flags & Flags.BATCH:
                    # One packet carries the items the sender had ready.
                    for item in payload:
                        yield item
                    owed_items += len(payload)
                else:
                    yield payload
                    owed_items += 1

                # The permission goes back when the next item is asked for, not
                # when the item reached the queue. Batches of half the budget
                # keep the sender busy without a message per item.
                owed_bytes += size
                if owed_bytes * 2 >= self.MAX_STREAM_WINDOW or owed_items * 2 >= self.MAX_STREAM_BACKLOG:
                    await self.send((owed_bytes, owed_items), Flags.RPC | Flags.STREAM | Flags.REQUEST, packet_id)
                    owed_bytes = 0
                    owed_items = 0
        finally:
            self.streams.pop(packet_id, None)
            self.streams_over_budget.discard(packet_id)
            self.stream_buffered.pop(packet_id, None)
            if not finished and not self._closed.is_set():
                # Tell the sender that nobody reads any more, so it stops
                # instead of producing items that go nowhere.
                try:
                    await self.send(None, Flags.RPC | Flags.STREAM | Flags.REQUEST, packet_id)
                except Exception:
                    logging.debug("Cannot report the end of stream %r", packet_id)


class RemoteLogHandler(logging.Handler):
    """Send records of this side to the peer, grouped into one packet.

    A record waits in a list until the loop runs the next step, and everything
    that gathered meanwhile travels together. One packet and one task then
    serve a burst instead of serving every record, and the loop keeps its time
    for the calls of the connection.

    A response that this side still owes takes the records with it, because
    that packet leaves anyway. A record of a served call then costs no packet
    at all. A long call must not hold its records, so they wait no longer than
    ATTACH_DELAY and then travel alone.

    One batch is on the wire at a time, in a response or in a packet of its
    own. The next batch waits for it, so the records of one logger arrive in
    the order they were written.
    """

    # Time a record waits for a response to carry it. A local round trip takes
    # about a tenth of this, so an ordinary call wins the race, and a call that
    # runs longer delays its records by this much only.
    ATTACH_DELAY: ClassVar[float] = 0.001

    # Limits of one attached batch. A response must keep room inside the frame
    # of FRAGMENT_SIZE bytes, so a burst does not make it a fragmented packet.
    ATTACH_RECORDS: ClassVar[int] = 64
    ATTACH_BYTES: ClassVar[int] = 16 * 1024

    def __init__(self, protocol: Protocol, loop: asyncio.AbstractEventLoop, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.protocol = protocol
        self.loop = loop
        # Each entry keeps the local record beside the dictionary that travels,
        # so a failure to send is reported against the record that caused it.
        self.pending: list[tuple[logging.LogRecord, LogRecord]] = []
        # True from the moment a batch is scheduled until the sender finds the
        # list empty. Both are changed under the lock of the handler.
        self.draining = False
        # True while a batch is on the wire. It keeps the batches in order.
        self.sending = False
        # Fallback of the records that wait for a response to carry them.
        self.timer: asyncio.TimerHandle | None = None

    def emit(self, record: logging.LogRecord) -> None:
        exc_text = record.exc_text
        if record.exc_info and not exc_text:
            formatter = self.formatter or logging.Formatter()
            exc_text = formatter.formatException(record.exc_info)
        self.pending.append(
            (record, (record.name, record.levelno, record.pathname, record.lineno, record.getMessage(), exc_text))
        )
        if self.draining:
            return
        self.draining = True
        # Synchronous Tool methods emit records from executor threads.
        try:
            self.loop.call_soon_threadsafe(self.start)
        except RuntimeError:
            self.draining = False
            self.pending.clear()
            self.handleError(record)

    def start(self) -> None:
        """Choose how the records that wait travel, on the loop thread."""
        if self.protocol._closed.is_set():
            self.acquire()
            try:
                self.draining = False
                self.pending.clear()
            finally:
                self.release()
            return
        if self.protocol.responses_pending and self.timer is None:
            # A response is owed and carries the records for free. The timer
            # sends them alone when no response leaves in time.
            self.timer = self.loop.call_later(self.ATTACH_DELAY, self.send_alone)
            return
        self.send_alone()

    def send_alone(self) -> None:
        """Send the records that wait in a packet of their own."""
        self.timer = None
        task = self.loop.create_task(self.drain())
        self.protocol._tasks.add(task)
        task.add_done_callback(self.protocol._tasks.discard)

    def detach(self) -> list[tuple[logging.LogRecord, LogRecord]]:
        """Take the records that wait, for a response to carry them.

        Gives an empty list when the records must not travel this way: another
        batch is on the wire, or the batch is too large for the response to
        stay in one frame. The records then keep their place and their own
        packet. The caller reports the result with sent() or restore().
        """
        self.acquire()
        try:
            if self.sending or not self.pending or len(self.pending) > self.ATTACH_RECORDS:
                return []
            if sum(self.weight(item[1]) for item in self.pending) > self.ATTACH_BYTES:
                return []
            batch, self.pending = self.pending, []
            self.sending = True
        finally:
            self.release()
        if self.timer is not None:
            # The response replaces the fallback. Only the loop thread detaches
            # a batch, so the timer is cancelled from the thread that owns it.
            self.timer.cancel()
            self.timer = None
        return batch

    @staticmethod
    def weight(record: LogRecord) -> int:
        """Report the text bytes of *record*, as an estimate of its size."""
        *_, message, traceback_text = record
        return len(message) + len(traceback_text or "")

    def sent(self) -> None:
        """Report that an attached batch reached the wire."""
        self.resume([])

    def restore(self, batch: list[tuple[logging.LogRecord, LogRecord]]) -> None:
        """Put an attached batch that never reached the wire back in its place."""
        self.resume(batch)

    def resume(self, batch: list[tuple[logging.LogRecord, LogRecord]]) -> None:
        """Free the wire for the next batch, and send what waits."""
        self.acquire()
        try:
            self.sending = False
            if batch:
                self.pending[:0] = batch
            again = bool(self.pending) and not self.draining
            if again:
                self.draining = True
            elif not self.pending:
                # Nothing waits any more, so the next record schedules its own
                # send. A record that finds draining set schedules nothing.
                self.draining = False
        finally:
            self.release()
        if again:
            self.start()

    async def drain(self) -> None:
        """Send everything that gathered, until nothing is left."""
        while True:
            self.acquire()
            try:
                if self.sending:
                    # A response carries the records that came first. It sends
                    # what is left after its own batch reaches the wire.
                    self.draining = False
                    return
                batch, self.pending = self.pending, []
                if not batch:
                    self.draining = False
                    return
                self.sending = True
            finally:
                self.release()
            try:
                # LOG IDs use the upper end of the unsigned packet ID field.
                await self.protocol.send([item[1] for item in batch], Flags.LOG, self.protocol.get_log_id())
            except Exception:
                self.acquire()
                try:
                    self.draining = False
                    self.sending = False
                finally:
                    self.release()
                for record, _ in batch:
                    self.handleError(record)
                return
            self.acquire()
            try:
                self.sending = False
            finally:
                self.release()


async def run() -> None:
    """remote endpoint entry point, do not call directly"""
    proto = await Protocol.from_stdio()
    async with proto:
        await proto.wait_closed()
