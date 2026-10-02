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

## Keep Tool and Client Code Separate

For a class defined at module level, rmote transfers the **whole module**,
including imports, helper functions, constants, and other classes. The target
executes its top-level statements when it loads the module.

Keep connections and local setup in the client file. The tool module should
contain definitions and imports, not deployment actions at import time.
Import it by its normal module name, as the client above does.

Imports from `rmote.protocol` work on the target. Other `rmote.*` imports are
removed from the transferred source. Other dependencies must already exist on
the target; transferring one module does not transfer all modules it imports.
The code must support the target's Python version.

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

Use `def` for blocking filesystem, subprocess, or library calls. rmote runs
these methods in worker threads. Use `async def` for operations that await
asynchronous APIs; those methods run on the target's event loop.

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

Use `process` from `rmote.protocol` inside a tool. Set `capture_output=True`
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
shell features. The helper blocks, so call it from a `def` method or through
`asyncio.to_thread` in an async method.

The target's standard descriptors are redirected to `/dev/null` to protect
the protocol. Uncaptured command output and remote `print()` calls are discarded.
Return output as data or use Python logging for events. See {doc}`concepts`
for logging callbacks and client thread ownership.

## State and Repeated Calls

Tools remain loaded on a connection. Changing local source or class variables
does not update an already loaded remote tool; open a new connection to load
new definitions. State is not shared automatically between connections.

Use a `classmethod` when a method needs class-level configuration. Prefer
explicit arguments when values vary per call. Calls can overlap, including
worker-thread methods and async methods. Protect shared mutable state or avoid it.

Module tools use module-qualified names, so classes with the same name in
different modules remain distinct.

## Define an Inline Tool

For a small script, define a tool inside a function. rmote transfers only the
class source, so method imports must be inside the methods. Closure values
and the surrounding function's globals are not transferred.

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

Inline tools use bare class names. Two different inline tools with the same
name can collide on a connection. Use module tools for reusable operations
and shared result types.
