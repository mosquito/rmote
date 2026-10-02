# Writing Tools

A *Tool* is a Python class whose methods execute in the remote interpreter.
Define the class locally and pass a method to a client. The first call transfers
the source; later calls reuse the loaded tool on that connection.
A new connection loads the tool again on its first call.

The client interface and the remote method are separate choices. Both the
synchronous `Connection` and the asynchronous `Protocol` can call `def` and
`async def` methods. The same tool works with either client.

## Define a Tool Module

Keep tool definitions in an importable `.py` module. Use a separate client script
for connections and local setup. For example, save this module as `inventory.py`:

<!-- name: test_inventory_module -->
```python
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rmote.protocol import Tool


@dataclass
class FileInfo:
    path: str
    size: int


class Inventory(Tool):
    encoding: ClassVar[str] = "utf-8"

    @classmethod
    def read_text(cls, path: str) -> str:
        return Path(path).read_text(encoding=cls.encoding)

    @classmethod
    async def read_text_async(cls, path: str) -> str:
        return await asyncio.to_thread(cls.read_text, path)

    @staticmethod
    def stat(path: str) -> FileInfo:
        return FileInfo(path=path, size=Path(path).stat().st_size)

    @classmethod
    def name(cls) -> str:
        return cls.__name__
```

Each tool must inherit from {class}`~rmote.protocol.Tool`. Use static or class
methods and pass operation inputs as arguments. The metaclass rejects an
explicit `__init__` with `TypeError`.

File paths passed to a tool refer to the remote host. The following examples
use a local subprocess, so the client and remote interpreter share a filesystem.

## Call the Same Tool with Either Client

### Synchronous client

Save this code as `client_sync.py` beside `inventory.py`. The factory opens the
connection; the `with` statement closes it when the block exits.

<!-- name: test_inventory_sync; fixtures: inventory_module, __name__; marks: timeout(10) -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory

from inventory import FileInfo, Inventory
from rmote.sync import Connection


def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "sample.txt"
        path.write_text("hello\n", encoding="utf-8")

        with Connection.from_local(rpc_timeout=5.0) as remote:
            assert remote(Inventory.read_text, str(path)) == "hello\n"
            assert remote(Inventory.read_text_async, str(path)) == "hello\n"
            info = remote(Inventory.stat, str(path))
            assert isinstance(info, FileInfo)
            assert info.size == 6
            assert remote(Inventory.name) == "Inventory"

            try:
                remote(Inventory.read_text, str(path) + ".missing")
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("The missing file must raise FileNotFoundError")


if __name__ == "__main__":
    main()
```

The async tool method still runs on the remote event loop. The synchronous client
waits for its result without requiring `await` in the client script.
Use `Connection.from_ssh(...)` for a remote host; see {doc}`quickstart` for
connection parameters and lifecycle details.

### Asynchronous client

Save this code as `client_async.py` beside `inventory.py`. The existing async API
uses the same tool and return types:

<!-- name: test_inventory_async; fixtures: inventory_module, __name__; marks: timeout(10) -->
```python
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from inventory import FileInfo, Inventory
from rmote.protocol import Protocol


async def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "sample.txt"
        path.write_text("hello\n", encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-qui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with await Protocol.from_subprocess(process) as remote:
                assert await remote(Inventory.read_text, str(path)) == "hello\n"
                assert await remote(Inventory.read_text_async, str(path)) == "hello\n"
                info = await remote(Inventory.stat, str(path))
                assert isinstance(info, FileInfo)
                assert info.size == 6
                assert await remote(Inventory.name) == "Inventory"

                try:
                    await remote(Inventory.read_text, str(path) + ".missing")
                except FileNotFoundError:
                    pass
                else:
                    raise AssertionError("The missing file must raise FileNotFoundError")
        finally:
            # from_subprocess closes the protocol; the caller owns the process.
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
```

## Choose `def` or `async def` for the Remote Work

| Tool method | Remote execution | Suitable work |
|---|---|---|
| `def` | A worker thread, through `asyncio.to_thread` | Blocking filesystem, subprocess, or library calls |
| `async def` | The remote event loop | Awaitable operations and async coordination |

Declaring a method `async def` does not make blocking I/O asynchronous.
For example, `urllib.request.urlopen()` blocks even inside an async method.
Use a `def` method for that operation, or move it into `asyncio.to_thread()`.
The `Inventory.read_text_async` example uses this approach for a filesystem read.

Do not call `time.sleep()`, `subprocess.run()`, or other blocking functions
directly in an async method. While that call runs, the remote loop cannot
process other RPCs or send their responses.

## Module Tools and Inline Tools

### Module tools

For a tool defined at module level, rmote reads and transfers the **whole module**.
Module-level stdlib imports, helper functions, constants, dataclasses, and other
tool classes in that file are available remotely. `inventory.py` uses this form.
Imports of `rmote.*` other than `rmote.protocol` are stripped from the transferred
source; client-side modules are not part of the injected runtime.

The remote interpreter executes the module's top-level statements when it loads
the module. Avoid top-level connections, file writes, or local initialization.
Keep client code in a separate script protected by
`if __name__ == "__main__":`. Import the tool module by its normal module name.

Transferring a module does not transfer every module that it imports.
Keep supporting code in the tool module, use stdlib dependencies, or ensure
that additional dependencies already exist remotely. rmote does not install them.
The source and dependencies must also support the remote Python version.

Tools in different modules use module-qualified names, such as
`inventory.Inventory`. Two module tools with the same class name therefore
remain distinct. A loaded module and its tools remain cached in that remote
interpreter. Editing the local source does not update an existing connection;
open a new connection to load the updated definitions.

### Inline tools

An *inline tool* is a class defined **inside a function**, not simply a class
in the same file as the client. rmote transfers only that class's source.
The surrounding function, closure values, and module globals are not transferred.

Put required imports inside the methods. `Tool` and the protocol helpers are
available in the reconstructed namespace. Return built-in picklable values for
this pattern; put shared custom types in a module tool instead.

This complete inline example needs no extra module:

<!-- name: test_inline_both_methods; subprocess: true; marks: timeout(10) -->
```python
from rmote.protocol import Tool
from rmote.sync import Connection


def main() -> None:
    class Echo(Tool):
        @staticmethod
        def echo(value: str) -> str:
            return value

        @staticmethod
        async def later(value: str) -> str:
            import asyncio

            await asyncio.sleep(0)
            return value

    with Connection.from_local(rpc_timeout=5.0) as remote:
        assert remote(Echo.echo, "sync method") == "sync method"
        assert remote(Echo.later, "async method") == "async method"


if __name__ == "__main__":
    main()
```

Inline tools use a bare class name. Two different inline classes with the same
name can collide on one connection. Use module tools when names or supporting
types need stable identities.

Source extraction needs a readable source file. Definitions from a REPL,
`exec()` string, or notebook are not a portable way to supply tools.
Keep production tools in `.py` files.

(returning-custom-types)=
## Arguments, Results, and Exceptions

The protocol uses `pickle` for arguments, results, and remote exceptions.
Built-in values such as strings, bytes, numbers, lists, and dictionaries work
when their contents are also picklable. Dataclasses can provide structured
results, as `FileInfo` does in the example.

Define custom types in the transferred tool module and import that same module
locally before making calls. This gives `pickle` the same module and class names
on both sides. Module-level dataclasses are convenient; nested dataclasses can
also work when the containing tool module is transferred.
An external custom type needs its defining module on both sides.

Do not pass or return open files, generators, coroutine objects, event loops,
or other objects that `pickle` cannot serialize. Return data instead of a
live local or remote resource. A tool exception such as `FileNotFoundError`
is raised in the client, as shown by both client examples.

Use rmote only with trusted peers and trusted tool source. The remote executes
transferred Python code, and unpickling data can execute Python code locally.
The wire format does not provide a sandbox for an untrusted endpoint.

## Class Variables and Concurrent Calls

`ClassVar` documents class-level configuration, such as `Inventory.encoding`.
Its value comes from the transferred source. Changing the local class variable
after synchronization does not change the remote value.

Remote class or module state can remain between calls on a connection.
It does not automatically survive a new remote process or synchronize with
another connection. Prefer explicit arguments and returned data when possible.

RPCs can overlap, including calls from different local threads. A `def` method
can run alongside another worker-thread method or an async method. Protect
shared mutable state or avoid it. An async method can also interleave with
other methods whenever it awaits.

## Run Subprocesses and Return Their Output

During bootstrap, `from_stdio` duplicates the protocol descriptors and redirects
the remote process's standard descriptors to `/dev/null`. The duplicated
descriptors are not inherited by child processes. A child using default stdio
therefore does not inherit the protocol pipes, but its output is discarded.

Use {func}`rmote.protocol.process` to set command I/O explicitly. It is a blocking
helper intended for `def` methods; use `asyncio.to_thread()` from an async method.
Without `stdin`, it sets `stdin=DEVNULL`. With `capture_output=True`, it captures
stdout and stderr; otherwise it redirects both to `DEVNULL`.

For example, save this as a separate tool module:

<!-- name: test_command_module -->
```python
import sys

from rmote.protocol import Tool, process


class Commands(Tool):
    @staticmethod
    def python_output() -> str:
        completed = process(
            sys.executable, "-c", "print('ready')",
            capture_output=True, text=True, check=True,
        )
        return completed.stdout
```

`python_output()` returns `"ready\n"`. `check=True` raises
`subprocess.CalledProcessError` for a nonzero exit status. Avoid shell execution
when passing untrusted arguments; pass the executable and each argument separately.

Remote `print()` output is discarded by the stdio protection. Return command
output as data. Use Python `logging` for remote events that the client should
receive. Local handlers for those records run in the synchronous client's
runtime thread; they must be thread-safe and must not call the blocking API
of that same connection.
