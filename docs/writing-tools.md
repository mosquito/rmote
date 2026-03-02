# Writing Tools

A *Tool* is a Python class whose methods execute on the remote side.
The class definition is serialized and transferred to the remote interpreter the first time any
method on the class is called through a connection - not when the class is defined, and not when
the connection is opened. This is called *lazy sync*.

Once a tool has been synced over a connection, every subsequent call skips the transfer entirely:
only the RPC packet (method name + arguments) is sent. If the connection is closed and a new one
is opened, the class is re-synced on its first call through the new connection.

## Rules

1. **Inherit from** {class}`~rmote.protocol.Tool`.
2. **No** `__init__` - the metaclass raises `TypeError` if one is defined.
3. **Static or class methods only** - instance state is not preserved across calls.
4. **Stdlib imports only** - the remote side has no installed packages.
5. **Import inside methods** - put `import` statements inside the method body so they
   are executed on the remote interpreter, not the local one.

## Name Collisions

Tools are identified on the remote side by their **module-qualified name** (`module.ClassName`),
not the bare class name.  Two Tool subclasses with the same class name in different modules work
correctly — each is stored and dispatched independently.

For example, suppose two modules both define a class called ``Helper``:

<!-- name: test_name_collision -->
```python
from rmote.protocol import Tool


# In a real project these would live in separate files,
# e.g. tools/monitoring.py and tools/deploy.py

class MonitoringHelper(Tool):
    @staticmethod
    def value() -> str:
        return "monitoring"


class DeployHelper(Tool):
    @staticmethod
    def value() -> str:
        return "deploy"
```

Both can be used in the same session without conflict:

<!-- name: test_name_collision -->
```python
import asyncio
import sys
from rmote.protocol import Protocol


async def main() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    async with await Protocol.from_subprocess(process) as proto:
        result_m = await proto(MonitoringHelper.value)
        result_d = await proto(DeployHelper.value)
        assert result_m == "monitoring"
        assert result_d == "deploy"


if __name__ == "__main__":
    asyncio.run(main())
```

Inline tools (defined inside a function body) use only the bare class name, so two inline tools
with the same name in the same session will collide.  This is rarely an issue in practice.

## Running Subprocesses

The remote Python process communicates with the local side over its own **stdin / stdout** as a
binary packet stream.  Any child process that inherits the default file descriptors will share
those pipes, and anything the child writes to stdout - even a single byte - will corrupt the
packet framing and break the connection permanently.

```{danger}
Never use ``subprocess.run``, ``subprocess.Popen``, or ``os.system`` directly inside a Tool
method.  They inherit the parent's stdin/stdout by default, which are the protocol pipes.
```

Use {func}`rmote.protocol.process` instead.  It always sets ``stdin=DEVNULL`` and, unless
``capture_output=True``, also redirects ``stdout`` and ``stderr`` to ``DEVNULL``:

<!-- name: test_process_basic -->
```python
from rmote.protocol import Tool, process


class DeployTool(Tool):
    @staticmethod
    def git_pull(repo: str) -> int:
        """Pull latest changes; return the exit code."""
        result = process("git", "-C", repo, "pull", "--ff-only")
        return result.returncode

    @staticmethod
    def capture(cmd: str) -> str:
        """Run a shell command and return its stdout."""
        result = process(cmd, shell=True, capture_output=True, text=True, check=True)
        return result.stdout
```

An *inline tool* is a Tool subclass defined in the same script or module that imports
``Protocol``.  A *file-level tool* is defined in its own standalone module that is imported
separately.

``process`` is injected into every tool namespace automatically - no import is needed for inline
tools.  For file-level tools add ``from rmote.protocol import process`` at the top of the file.

### What `process` does

| Default behaviour | Why |
|---|---|
| `stdin=DEVNULL` | Child cannot read protocol data |
| `stdout=DEVNULL` (unless `capture_output=True`) | Child cannot corrupt the protocol stream |
| `stderr=DEVNULL` (unless `capture_output=True`) | Remote stderr is also the protocol channel |

The signature mirrors `subprocess.run` for everything else: `check`, `env`, `cwd`, `shell`, and
an optional `stdin` argument (bytes or str) for data that should be piped *into* the child.

### What not to do

<!-- name: test_process_comparison -->
```python
import os
import subprocess

from rmote.protocol import process


if __name__ == "__main__":
   # BAD - child inherits the protocol pipes

   subprocess.run(["apt-get", "update"])

   # BAD - os.system goes through the shell which inherits the same fds
   os.system("apt-get update")
   
   # GOOD
   process("apt-get", "update")
```

## Sync and Async Methods

Both sync and async methods are supported.
The protocol detects the method type via `inspect.iscoroutinefunction` and dispatches
accordingly:

<!-- name: test_sync_async_methods -->
```python
import urllib.request
from rmote.protocol import Tool


class MyTool(Tool):
    @staticmethod
    def read_file(path: str) -> str:
        """Synchronous - runs in a thread on the remote."""
        with open(path) as f:
            return f.read()

    @staticmethod
    async def fetch(url: str) -> bytes:
        """Async - runs directly in the remote event loop."""
        with urllib.request.urlopen(url) as resp:
            return resp.read()
```

## Class Variables

Class variables can hold configuration.
Annotate them as `typing.ClassVar` to signal that they are not instance attributes:

<!-- name: test_class_variables -->
```python
from typing import ClassVar
from rmote.protocol import Tool


class Config(Tool):
    base_url: ClassVar[str] = "https://example.com"

    @classmethod
    def get_url(cls, path: str) -> str:
        return cls.base_url + path
```

(returning-custom-types)=
## Returning Custom Types

Any picklable object can be returned - including dataclasses:

<!-- name: test_custom_return_types -->
```python
import os
import dataclasses
from rmote.protocol import Tool


@dataclasses.dataclass
class FileInfo:
    path: str
    size: int


class Inspector(Tool):
    @staticmethod
    def stat(path: str) -> "FileInfo":
        s = os.stat(path)
        return FileInfo(path=path, size=s.st_size)
```

```{note}
The dataclass (or any custom type you return) must be defined *outside* the `Tool`
class body so it is available both in the tool source sent to the remote side and in
the local namespace where the result is unpickled.
```

## Complete Example

<!-- name: test_complete_example -->
```python
import asyncio
import dataclasses
import shutil
import socket
from rmote.protocol import Protocol, Tool


@dataclasses.dataclass
class DiskUsage:
    path: str
    total: int
    used: int
    free: int


class SystemInfo(Tool):
    @staticmethod
    def disk_usage(path: str = "/") -> "DiskUsage":
        """Return disk usage statistics for *path*."""
        total, used, free = shutil.disk_usage(path)
        return DiskUsage(path=path, total=total, used=used, free=free)

    @staticmethod
    def hostname() -> str:
        """Return the remote machine hostname."""
        return socket.gethostname()

    @classmethod
    def name(cls) -> str:
        return cls.__name__


async def main() -> None:
    process = await asyncio.create_subprocess_exec(
        "python3", "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    proto = await Protocol.from_subprocess(process)

    async with proto:
        host = await proto(SystemInfo.hostname)
        du = await proto(SystemInfo.disk_usage, "/")
        print(f"{host}: {du.free // 2**30} GB free on /")


if __name__ == "__main__":
    asyncio.run(main())
```

`Protocol.from_subprocess` bootstraps the remote process, then each `await proto(...)` first
syncs the tool class (once) and then issues the RPC call. Because the two calls are sequential,
the second call reuses the already-synced `SystemInfo` class:

```{mermaid}
sequenceDiagram
    participant L as Local
    participant R as python3 subprocess

    Note over L,R: Protocol.from_subprocess - bootstrap
    L->>R: exec(decompress(b64decode(payload)))
    R-->>L: PROTOCOL READY

    Note over L,R: await proto(SystemInfo.hostname) - first call
    L->>R: SYNC SystemInfo source
    R-->>L: ACK
    L->>R: REQUEST {hostname, id=1}
    R-->>L: RESPONSE {"web01", id=1}

    Note over L,R: await proto(SystemInfo.disk_usage, "/") - sync skipped
    L->>R: REQUEST {disk_usage, "/", id=2}
    R-->>L: RESPONSE {DiskUsage(path="/", ...), id=2}
```
