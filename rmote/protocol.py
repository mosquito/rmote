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
import re
import subprocess
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
        and nested ``{}`` inside expressions. Python string tokens do not
        change the brace depth.
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
                start = i
                try:
                    tokens = tokenize.generate_tokens(io.StringIO(line[start:]).readline)
                    for token in tokens:
                        if token.type != tokenize.OP:
                            continue
                        if token.string == "{":
                            depth += 1
                        elif token.string == "}":
                            depth -= 1
                            if depth == 0:
                                end = start + token.start[1]
                                result.append((True, line[start:end]))
                                i = end + 1
                                break
                    else:
                        raise SyntaxError("Unclosed template expression")
                except tokenize.TokenError as exc:
                    raise SyntaxError("Invalid template expression") from exc
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
        the rendered string. Compiled functions are cached by source text
        within this process. Rendering still executes the function.
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


class RPCRequest(TypedDict):
    method: str
    args: Any  # Can be tuple[Any, ...] or P.args
    kwargs: dict[str, Any]
    compressed: NotRequired[bool]


class LogRecord(TypedDict):
    name: str
    levelno: int
    levelname: str
    pathname: str
    lineno: int
    msg: str
    args: Any
    exc_info: Any
    exc_text: NotRequired[str | None]


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
                chunk = await self.reader.readexactly(length)

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

        The COMPRESSED flag travels with every frame, so the sender alone
        decides and the peer needs no agreement about the policy.
        """
        if self._compression.get() and len(payload) > self.COMPRESSION_THRESHOLD:
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
        """Write one frame as a single unit. Hold the write lock for that frame."""
        async with self.write_lock:
            header = self.PACKET_HEADER.pack(self.MAGIC, flags, len(payload), packet_id)
            if self.writer.is_closing():
                raise ConnectionError("Connection closed")
            try:
                self.writer.write(header + payload)
                await self.writer.drain()
            except BaseException:
                # The peer may have received only part of the packet.
                self.writer.close()
                raise

    async def write_boundary(self) -> None:
        async with self.write_lock:
            self.writer.write(self.BOUNDARY)
            await self.writer.drain()

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
        # The key the peer registered every tool under, by class or module.
        self._tools_cache: dict[type[Tool] | ModuleType, str] = {}
        # Keyed by the name of a bundle root, or by a class that has no bundle.
        self._tool_sync_locks: dict[Any, asyncio.Lock] = {}
        self._tool_load_lock = asyncio.Lock()
        self.futures: dict[int, asyncio.Future[Any]] = {}
        self.loop = asyncio.get_running_loop()
        self._loop_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._close_error: Exception | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.tools: dict[str, Tool | ModuleType] = dict()
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
        await self.write_boundary()
        await self.read_boundary()
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
            # The close does not wait for this task: it reads a pipe that the
            # transport closes when it ends.
            self._stderr_task.cancel()
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
            await self._owned_process.wait()
            self._owned_process = None

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

        if inspect.iscoroutinefunction(method):
            return await method(*request["args"], **request["kwargs"])

        if runs_on_loop(method):
            # The author promised the method does not wait, so it is cheaper
            # to call it here than to wake a worker thread for it.
            return method(*request["args"], **request["kwargs"])

        return await asyncio.to_thread(method, *request["args"], **request["kwargs"])




    async def _handle_rpc_response(self, response: Any, packet_id: int) -> None:
        if packet_id not in self.futures:
            logging.warning("RPC response %r packet not found in futures", packet_id)
            return
        future = self.futures.pop(packet_id)
        if not future.done():
            future.set_result(response)

    async def _handle_exception(self, exception: Exception, packet_id: int) -> None:
        if packet_id not in self.futures:
            logging.warning("Exception response %r packet not found in futures: %s", packet_id, exception)
            return
        future = self.futures.pop(packet_id)
        if not future.done():
            future.set_exception(exception)

    @staticmethod
    async def _handle_log(record: LogRecord, _: int) -> None:
        """Deliver a remote record to local handlers in the protocol loop thread."""
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
        log_record.exc_text = record.get("exc_text")
        logger.handle(log_record)

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

        task = asyncio.create_task(wrapper())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_response(self, response: Any, flags: Flags, packet_id: int) -> None:
        """Send a response or a portable serialization error; close after a transport failure."""
        try:
            await self.send(response, flags, packet_id)
        except Exception as error:
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

    async def _loop(self) -> None:
        error: Exception | None = None
        try:
            while not self._closed.is_set():
                packet = await self.receive()
                payload, flags, packet_id = packet.payload, packet.flags, packet.packet_id
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
                    self._execute(packet_id, Flags.LOG, self._handle_log, payload)
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

    @overload
    async def __call__(self, tool: Callable[P, Coroutine[Any, Any, R]], *args: P.args, **kwargs: P.kwargs) -> R: ...

    @overload
    async def __call__(self, tool: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R: ...

    async def __call__(self, tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await self._call_tool(tool, *args, **kwargs)

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



class RemoteLogHandler(logging.Handler):
    def __init__(self, protocol: Protocol, loop: asyncio.AbstractEventLoop, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.protocol = protocol
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        exc_text = record.exc_text
        if record.exc_info and not exc_text:
            formatter = self.formatter or logging.Formatter()
            exc_text = formatter.formatException(record.exc_info)
        record_dict = LogRecord(
            name=record.name,
            levelno=record.levelno,
            levelname=record.levelname,
            pathname=record.pathname,
            lineno=record.lineno,
            msg=record.getMessage(),
            args=(),
            exc_info=None,
            exc_text=exc_text,
        )

        async def send_record() -> None:
            try:
                # LOG IDs use the upper end of the unsigned packet ID field.
                await self.protocol.send(record_dict, Flags.LOG, self.protocol.get_log_id())
            except Exception:
                self.handleError(record)

        def schedule() -> None:
            if self.protocol._closed.is_set():
                return
            task = self.loop.create_task(send_record())
            self.protocol._tasks.add(task)
            task.add_done_callback(self.protocol._tasks.discard)

        # Synchronous Tool methods emit records from executor threads.
        try:
            self.loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            self.handleError(record)


async def run() -> None:
    """remote endpoint entry point, do not call directly"""
    proto = await Protocol.from_stdio()
    async with proto:
        await proto.wait_closed()
