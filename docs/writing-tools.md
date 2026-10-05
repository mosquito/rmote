# Writing Tools

A tool groups Python functions that run on the target machine. Start with one
operation, then add parameters and return types as your application needs them.
The examples below check disk space. They use a local subprocess first so you
can run them without an SSH server.

## Define One Operation

Save this as `disk_tools.py`:

```{literalinclude} ../examples/tools/disk_tools.py
:language: python
```

Inherit from `Tool` and define a static method. Its arguments describe the work;
its return value is the data sent back to the caller. Do not define `__init__`:
tools are registered classes, not objects that you construct with local state.

Save the following client beside `disk_tools.py` and run it with Python:

<!-- name: test_tools; case: first_call; fixtures: tool_examples, client_resources; mark: timeout(20) -->
```python
from disk_tools import Disk
from rmote.sync import Connection

with Connection.from_local() as remote:
    free = remote(Disk.free_bytes, "/")
    assert isinstance(free, int) and free >= 0
    print(f"Free space: {free // (1024 ** 3)} GiB")
```

Replace `Connection.from_local()` with `Connection.from_ssh("user@server")`
to check another machine. The path is interpreted on that machine.
rmote sends `disk_tools.py` when `Disk` is first called on the connection.

## Modules and packages as tools

A tool can also be an ordinary importable module. Define a top-level function
in `mytools.py`, then call `await remote(mytools.function, ...)`. Async functions
and async generators work through the same clients, preserving their type hints.
Functions defined in `__main__`, closures, lambdas and arbitrary bound methods
are not module tools. Existing Tool classes keep their usual behavior.

When the function belongs to a package's `__init__.py`, rmote sends all `.py`
files in that regular package and its regular subpackages. For a function in a
single module, only that module is sent by default. Set an explicit package
boundary in the defining module when it needs siblings:

```{code-block} python
# mytools/operations.py
__tool_package__ = "mytools"
from .helpers import calculate

def run(value: int) -> int:
    return calculate(value)
```

`await remote(operations.run, 42)` then carries the `mytools` package. Imports
are retained, including `rmote.*` imports. All source names are registered before
execution; a standard import loader executes modules on demand, with correct
package metadata and normal Python import locks. There is no topological sort.
Circular imports have ordinary Python semantics, including errors when a
from-import requests a name that has not been defined yet. Failed imports can
be retried; successfully imported dependencies remain loaded.

Missing ancestors outside the bundle become empty parent packages. Their
`__init__.py` code is not transferred or executed, so include the needed ancestor
in the explicit boundary if it defines required behavior. External dependencies
must be installed on the target or provided by the bootstrap. This mechanism
transfers Python sources, not package resources, native extensions, namespace
packages, wheels or the controller's installed environment.

Source bundles are immutable within a remote interpreter. Conflicting bundles
or collisions with modules loaded outside the transfer mechanism fail explicitly;
restart the controller after changing source. Source collection uses a
process-wide LRU cache shared by connections, with up to 128 module/package
entries. The transferred module names also
let pickle restore dataclass results on the controller. The controller must have
the same importable model definitions. Runtime mutations of globals are not sent.

### Declared dependencies and object arguments

A module can declare `__tool_dependencies__ = ("another_package",)` to include
additional modules or packages explicitly. Each dependency may declare its own
dependencies; repeated names and cycles are collected only once. Imports outside
these boundaries still require code already installed on the target.

Modules that declare `__tool_package__` or `__tool_dependencies__` also opt their
classes and functions into dependency transfer when used in RPC arguments or
results. The pickler discovers them while traversing the object graph, including
nested containers and reducer functions. The packet carries a source manifest
outside the object pickle. The receiver registers sources before decoding the
objects; imports remain lazy. Connections remember successfully transferred
modules, so later calls do not resend their sources. A file-based Tool class in
an argument travels the same way: the payload holds its module and qualified
name, and its bundle goes in the manifest, so a class the peer already has
costs no more than its name. An inline class has no module to import and keeps
its source in the payload. Streaming budgets include
the first item's source manifest when one is required.

Class tools collect declared dependencies referenced in their source and import
statements. This supports `Template` in tool code, as well as an already compiled
`Template` nested in arguments to an otherwise unrelated tool. Dependency transfer
does not copy runtime global changes or recompile a template's stored program.

## Keep Tool and Client Code Separate

For a class defined at module level, rmote transfers the **whole module**,
including imports, helper functions, constants, and other classes. The target
executes its top-level statements when it loads the module.

Keep connections and local setup in the client file. The tool module should
contain definitions and imports, not deployment actions at import time.
Import it by its normal module name, as the client above does.

`rmote.protocol` is available immediately after bootstrap. Template imports
through `rmote.templates`, `rmote.template` and `rmote.filters` are transferred
lazily when referenced by a tool or its arguments.
File-based classes and module functions share the same source loader. Imports
are preserved. Referenced Tool classes and explicitly transferable modules are
included as dependencies; other dependencies must already exist on the target.
Use the defining module when importing a class, for example
`from rmote.tools.fs import FileSystem`. Re-exports in an ancestor package require
that ancestor's initializer inside the transferred package boundary.
The code must support the target's Python version.

rmote transfers source definitions, not the client's live Python state. Local
changes to module globals or class attributes are not copied with the source;
pass values as method arguments when they must reach the target. A direct call
such as `Disk.free_bytes("/")` runs locally. Calling it through `remote(...)`
performs the transfer and remote execution described in {doc}`concepts`.

Keep tool source in readable `.py` files. Definitions in a REPL, notebook, or
`exec()` string are not a portable way to provide source for transfer.

(returning-custom-types)=
## Return Structured Data

Use a dataclass when an operation returns several related values. Save this
expanded tool as `disk_info.py`:

```{literalinclude} ../examples/tools/disk_info.py
:language: python
```

Import the result type from the same module in your client:

<!-- name: test_tools; case: structured_result -->
```python
from disk_info import DiskInfo, DiskSpace

with Connection.from_local() as remote:
    usage = remote(DiskInfo.usage, "/")
    assert isinstance(usage, DiskSpace)
    assert usage.total > 0 and usage.free >= 0 and usage.used >= 0
    print(f"Used: {usage.used}; available: {usage.free}")
```

Arguments, results, and exceptions travel through pickle. Built-in values such
as strings, integers, lists, and dictionaries work when their contents are
picklable. Custom types need the same module and class names on both sides.
Defining a result type in the tool module provides that shared definition.
An external type requires its defining module on both machines.

Return data rather than open files, generators, coroutines, or event loops.
Use rmote only with trusted peers and tool code: executing transferred code and
unpickling results can execute Python on the receiving machine.

## Handle Remote Errors

A tool exception is raised in the client. Handle it around the call:

<!-- name: test_tools; case: remote_error -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory() as directory:
    missing = str(Path(directory) / "missing")
    with Connection.from_local() as remote:
        try:
            remote(Disk.free_bytes, missing)
        except FileNotFoundError:
            print("The target path does not exist.")
        else:
            raise AssertionError("The missing path must raise FileNotFoundError")
        assert remote(Disk.free_bytes, "/") >= 0
```

The temporary path makes this local example reproducible. With SSH, use a path
on the target instead. An operation error does not necessarily close the
connection; a transport failure requires a new connection.

## Choose a Method for the Work

Use `def` for blocking filesystem or library calls. rmote runs
these methods in worker threads. Use `async def` for operations that await
asynchronous APIs; those methods run on the target's event loop.

A method that only reads a field, formats a value or returns a constant cannot
block, and the hand-off to a worker thread then costs more than the call: about
30 µs against a fraction of one. Mark such a method with
{func}`rmote.protocol.inline`, and it runs on the target's event loop like
an async method. Use the mark only when the method cannot wait: no file, no
socket, no lock held by code that waits, and no long computation. A marked
method that waits stops every other call of that connection.

<!-- name: test_inline_mark -->
```python
from rmote.protocol import Tool, inline


class Release(Tool):
    @staticmethod
    @inline
    def name() -> str:
        import platform

        return platform.python_version()
```

Declaring a method async does not make a blocking call asynchronous. If you
need blocking work inside an async method, move it into `asyncio.to_thread`.
For example, save this as `async_disk.py`:

```{literalinclude} ../examples/tools/async_disk.py
:language: python
```

Both client interfaces can call either kind of tool method. A synchronous
client waits for an async method's result:

<!-- name: test_tools; case: async_method -->
```python
from async_disk import AsyncDisk

with Connection.from_local() as remote:
    assert remote(AsyncDisk.free_bytes, "/") >= 0
```

In an async application, use `Protocol` and await the result. This complete
client uses the same tool over SSH:

<!-- name: test_tools_async; fixtures: tool_examples, docs_ssh; mark: timeout(20) -->
```python
import asyncio
from async_disk import AsyncDisk
from rmote.protocol import Protocol


async def main():
    async with await Protocol.from_ssh("user@server") as remote:
        free = await remote(AsyncDisk.free_bytes, "/")
        assert free >= 0
        print(free)


asyncio.run(main())
```

## Run a Command and Return Its Output

Use `await async_process(...)` from `rmote.process` inside an async tool
method. Set `capture_output=True`
to collect stdout and stderr, and `text=True` to decode them to strings.
Save this as `command_tools.py`:

```{literalinclude} ../examples/tools/command_tools.py
:language: python
```

Call it through a connection:

<!-- name: test_tools; case: command_output -->
```python
from command_tools import Commands

with Connection.from_local() as remote:
    version = remote(Commands.python_version)
    assert version.startswith("Python 3.")
    print(version)
```

`check=True` raises `subprocess.CalledProcessError` for a nonzero exit status.
Pass the executable and arguments separately; use a shell only when you need
shell features. `async_process` awaits the child without blocking the target
event loop. An optional `timeout` limits communication after spawn and raises
`subprocess.TimeoutExpired`; timeout or local task cancellation kills the child
process group and reaps the direct child. This does not change RPC cancellation:
cancelling the client call still only stops local waiting. The synchronous
`process` helper remains available for synchronous tool methods.

The target's standard descriptors are redirected to `/dev/null` to protect
the protocol. Uncaptured command output and remote `print()` calls are discarded.
Return output as data or use Python logging for events. See {doc}`concepts`
for logging callbacks and client thread ownership.

## Stream a Long Result

A method written as an async generator streams its result. Every item travels as
its own response, so the remote side pushes the items as it produces them and
does not wait for a request per item. Use this for output that arrives over
time, such as a log tail or the output of a running process.

The client reads the items with `async for`. The call needs no await of its own,
because it returns the iterator:

```python
import asyncio
from collections.abc import AsyncIterator
from typing import Any

from rmote.protocol import Tool


class LogTail(Tool):
    @staticmethod
    async def follow(path: str) -> AsyncIterator[str]:
        """Yield new lines of a file as they appear."""
        with open(path) as handle:
            handle.seek(0, 2)
            while True:
                line = handle.readline()
                if not line:
                    await asyncio.sleep(0.2)
                    continue
                yield line.rstrip("\n")


async def print_new_lines(remote: Any, path: str) -> None:
    async for line in remote(LogTail.follow, path):
        print(line)
```

Annotate the method as returning `AsyncIterator[T]`. Without that annotation the
type checker reads the call as an ordinary one.

Three rules matter for a streaming method:

- A slow client slows the method. The bytes and the items in flight are both
  limited, so the generator waits inside its `yield` until the client reads.
  Memory stays bounded on both sides. Yield bounded blocks, because one item
  above the limit is refused.
- When the client leaves the loop, the sender stops: its permission is
  released and the task that produces the items is cancelled. The generator
  receives the cancellation wherever it waits, so its `finally` runs whether
  it was producing or waiting for data. A bare `break` does not close the
  iterator at once, so a client that needs an immediate close uses
  `contextlib.aclosing`. Synchronous code cannot be interrupted, because the
  cancellation arrives at the next `await`; a method that holds a resource
  between calls still needs a second method to release it.
- An exception inside the generator reaches the client after the items that
  came before it.

{doc}`api/tools/vty` is a complete example: it opens a process, streams its
output, and releases the session through a separate call.

## State and Repeated Calls

Tools remain loaded on a connection. Changing local source or class variables
does not update an already loaded remote tool. Restart the controller after
source changes to reload cached module/package bundles. State is not shared
automatically between connections.

Use a `classmethod` when a method needs class-level configuration. Prefer
explicit arguments when values vary per call. Calls can overlap, including
worker-thread methods and async methods. Protect shared mutable state or avoid it.

Module tools use module-qualified names, so classes with the same name in
different modules remain distinct.

## Define an Inline Tool

For a small script, define a tool inside a function, or at the top level of the
script. Both travel as source: a class of `__main__` has no module the target
could import. rmote transfers only the class source, so method imports must be
inside the methods. Closure values and the surrounding function's globals are
not transferred.

<!-- name: test_inline_tool; subprocess: true; mark: timeout(15) -->
```python
from rmote.protocol import Tool
from rmote.sync import Connection


def main():
    class Disk(Tool):
        @staticmethod
        def free_bytes(path: str) -> int:
            import shutil
            return shutil.disk_usage(path).free

    with Connection.from_local() as remote:
        assert remote(Disk.free_bytes, "/") >= 0


if __name__ == "__main__":
    main()
```

Inline tools use bare class names, and a class at the top level of a script is
an inline tool as well. Two different inline tools with the same name can
collide on a connection. Use module tools for reusable operations and shared
result types.
