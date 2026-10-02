# Quickstart

## Installation

```bash
pip install rmote
```

The local side requires Python 3.11 or newer. The remote interpreter needs only
its standard library. Install rmote on the local side.

## Choose a Client

| Client | Import | Connect | Call | Close |
|---|---|---|---|---|
| Synchronous | `from rmote.sync import Connection` | `Connection.from_local()` or `Connection.from_ssh(...)` | `remote(Tool.method, ...)` | `with remote` or `remote.close()` |
| Asynchronous | `from rmote.protocol import Protocol` | `await Protocol.from_subprocess(process)` or `await Protocol.from_ssh(...)` | `await remote(Tool.method, ...)` | `async with remote` |

Both clients can call `def` and `async def` Tool methods. The client interface
controls how your local code waits. It does not change remote execution.
The {doc}`writing-tools` guide explains tool modules, inline tools, and return types.

## Local Subprocess

These examples share a tool module. Save it as `lifecycle_tools.py` beside the
client scripts. Keep connections and local setup in the client scripts because
rmote transfers the tool module to the remote interpreter.

<!-- name: test_lifecycle_tool_module -->
```python
import asyncio
from rmote.protocol import Tool


class Echo(Tool):
    @staticmethod
    def echo(value: str) -> str:
        return value

    @staticmethod
    async def later(value: str, delay: float = 0.0) -> str:
        await asyncio.sleep(delay)
        return value
```

### Synchronous client

Save this as `client_sync.py`. The factory starts a local Python interpreter and
returns after the protocol handshake. The context closes its process and
background thread, including when an exception leaves the block.

<!-- name: test_local_sync; fixtures: lifecycle_module, __name__; marks: timeout(15) -->
```python
from lifecycle_tools import Echo
from rmote.sync import Connection


def main() -> None:
    with Connection.from_local(rpc_timeout=5.0) as remote:
        assert remote(Echo.echo, "hello") == "hello"
        assert remote(Echo.later, "again") == "again"
        assert remote.call_with_timeout(2.0, Echo.echo, "deadline") == "deadline"


if __name__ == "__main__":
    main()
```

Each connection loads a Tool on its first call. Later calls reuse the loaded
source. Keep the connection open across related operations.

### Asynchronous client

Save this as `client_async.py` beside the same tool module. The caller owns the
subprocess passed to `from_subprocess`. The `finally` block terminates and
reaps it after the protocol context closes.

<!-- name: test_local_async; fixtures: lifecycle_module, __name__; marks: timeout(15) -->
```python
import asyncio
import sys
from lifecycle_tools import Echo
from rmote.protocol import Protocol


async def main() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with await Protocol.from_subprocess(process) as remote:
            assert await remote(Echo.echo, "hello") == "hello"
            assert await remote(Echo.later, "again") == "again"
            async with asyncio.timeout(2.0):
                assert await remote(Echo.echo, "deadline") == "deadline"
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
```

The `-qui` flags select quiet, unbuffered, isolated interpreter mode.
For a process that can ignore termination, use a bounded wait and then kill it.
The synchronous `Connection` implements that process cleanup automatically.

Runnable versions are in `examples/local_sync.py`, `examples/local_async.py`,
and `examples/lifecycle_tools.py` in the repository.

## SSH Remote Process

### Synchronous client

Replace the local factory with {meth}`~rmote.sync.Connection.from_ssh`.
The connection owns the SSH subprocess and its background runtime.

<!-- name: test_ssh_sync; fixtures: docs_ssh; marks: timeout(20) -->
```python
from rmote.sync import Connection
from rmote.tools import FileSystem

with Connection.from_ssh("user@server", rpc_timeout=10.0) as remote:
    files = remote(FileSystem.glob, "/", "*")
    assert isinstance(files, list)
    print(files)
```

### Asynchronous client

The existing async SSH factory remains available. Its protocol context
completes the handshake and manages the owned SSH subprocess.

<!-- name: test_ssh_async; fixtures: docs_ssh, __name__; marks: timeout(20) -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main() -> None:
    async with await Protocol.from_ssh("user@server") as remote:
        files = await remote(FileSystem.glob, "/", "*")
        assert isinstance(files, list)
        print(files)


if __name__ == "__main__":
    asyncio.run(main())
```

### SSH options and jump hosts

Both factories accept `user`, `port`, `identity`, `python`, `ssh_options`, and
`stderr`. Paths refer to the remote host, except the local identity file.
`python` selects the remote interpreter. Extra SSH arguments precede the host.

<!-- name: test_ssh_sync_options; fixtures: docs_ssh; marks: timeout(20) -->
```python
from rmote.sync import Connection
from rmote.tools import FileSystem

with Connection.from_ssh(
    "myserver",
    user="deploy",
    port=2222,
    identity="/home/deploy/.ssh/id_ed25519",
    python="python3.11",
    ssh_options=["-o", "BatchMode=yes", "-J", "bastion.example.com"],
    connect_timeout=20.0,
    rpc_timeout=10.0,
    close_timeout=5.0,
) as remote:
    print(remote(FileSystem.glob, "/var/log", "*.log"))
```

For the async client, pass the same SSH arguments to `await Protocol.from_ssh(...)`.
The async factory does not accept the three synchronous deadline arguments.
Use `asyncio.timeout(...)` around the async operation that needs a deadline.

Pass `ssh_options=["-J", "bastion.example.com"]` for one jump host. For multiple
jumps, use a comma-separated value such as
`"deploy@bastion1.example.com:2222,relay@bastion2.internal"`.
The client communicates with the final destination through the SSH tunnel.

With synchronous `stderr=subprocess.PIPE`, the connection continuously reads
and discards stderr. Use inherited stderr, for example `stderr=2`, when you
need SSH diagnostics. With the async factory, the caller must arrange reading
when selecting `stderr=asyncio.subprocess.PIPE`.

## Deadlines, Errors, and Interruptions

| Synchronous argument | Default | Scope |
|---|---|---|
| `connect_timeout` | `30.0` seconds | Process creation, bootstrap, and handshake |
| `rpc_timeout` | `None` | Each call, including the first Tool upload, send, and response |
| `close_timeout` | `5.0` seconds | Graceful protocol and process shutdown |

`None` disables a connect or RPC deadline. Numeric deadlines must be finite and
positive. `close_timeout` must be finite and positive and cannot be `None`.
Invalid values raise `ValueError` before the factory creates resources.

Use `call_with_timeout(timeout, tool, /, *args, **kwargs)` to override one call's
RPC deadline. Both the deadline and method are positional. Every keyword
argument belongs to the remote method, including `timeout` and `tool`.

<!-- name: test_sync_deadline; fixtures: lifecycle_module, __name__; marks: timeout(15) -->
```python
from lifecycle_tools import Echo
from rmote.sync import Connection


def main() -> None:
    with Connection.from_local(rpc_timeout=5.0) as remote:
        try:
            remote.call_with_timeout(0.05, Echo.later, "slow", delay=0.5)
        except TimeoutError:
            print("Local waiting stopped; the remote operation can continue.")
        else:
            raise AssertionError("The delayed call must exceed its deadline")
        assert remote(Echo.echo, "still open") == "still open"


if __name__ == "__main__":
    main()
```

Timeout and `KeyboardInterrupt` stop local waiting and remove the pending
request. They do not cancel remote work. A remote write or package operation
can still complete. Cancellation while transmitting a packet can break the
channel; close the connection after a transport failure and create another.
Late responses to cancelled requests are ignored.

Remote exceptions are raised locally. Use `try` inside the context to handle
an expected operation error while keeping the connection open:

<!-- name: test_sync_remote_error; fixtures: __name__; marks: timeout(15) -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory
from rmote.sync import Connection
from rmote.tools import FileSystem


def main() -> None:
    with TemporaryDirectory() as directory:
        missing = str(Path(directory) / "missing.txt")
        with Connection.from_local(rpc_timeout=5.0) as remote:
            try:
                remote(FileSystem.read_str, missing)
            except FileNotFoundError:
                print("The remote file does not exist.")
            else:
                raise AssertionError("A missing file must raise FileNotFoundError")
            assert remote(FileSystem.glob, directory, "*") == []


if __name__ == "__main__":
    main()
```

Transport errors can include `ConnectionError`, `BrokenPipeError`, or EOF
errors. A failed connection does not reconnect automatically. A factory cleans
up partially created resources before propagating startup failure or interruption.

## Explicit Closing

Use `finally` when a context manager does not fit your application:

<!-- name: test_sync_explicit_close; fixtures: lifecycle_module, client_resources; marks: timeout(15) -->
```python
from lifecycle_tools import Echo
from rmote.sync import Connection

remote = Connection.from_local(rpc_timeout=5.0)
try:
    assert remote(Echo.echo, "first") == "first"
    assert remote(Echo.later, "second") == "second"
finally:
    remote.close()

remote.close()  # Repeated close is safe.
```

A synchronous connection owns one subprocess and one private background loop
thread. `close()` closes the protocol, reaps the process, and joins that thread.
After the graceful deadline, cleanup uses terminate, a one-second grace period,
then kill and wait. Local cancellation and executor shutdown must cooperate;
`close_timeout` does not impose a fixed total duration on `close()`.

Calls after closing and nested `with` entry raise `RuntimeError`.
The context does not suppress exceptions from its body. If cleanup also fails,
the body exception is preserved and the cleanup error is logged.

## Concurrent Calls and Async Applications

An open `Connection` accepts calls from multiple caller threads. Use
`ThreadPoolExecutor` to dispatch concurrent synchronous calls. For `Protocol`,
use `asyncio.gather`. See {doc}`multi-host` for both patterns and persistent sessions.

In an async application, prefer `Protocol`. If you need the synchronous client,
move its complete lifecycle into a worker thread:

<!-- name: test_sync_from_async; fixtures: lifecycle_module, __name__; marks: timeout(15) -->
```python
import asyncio
from lifecycle_tools import Echo
from rmote.sync import Connection


def fetch() -> str:
    with Connection.from_local(rpc_timeout=5.0) as remote:
        return remote(Echo.echo, "hello")


async def main() -> None:
    assert await asyncio.to_thread(fetch) == "hello"


if __name__ == "__main__":
    asyncio.run(main())
```

Direct synchronous calls block the calling event loop. Cancelling an await of
`asyncio.to_thread` does not stop its worker; set an RPC deadline and retain
ownership until the worker finishes.

Remote log records invoke local handlers in the connection's background loop
thread. Handlers must be thread safe. They must not call that connection's
synchronous methods, which raise `RuntimeError` from its own loop thread.

## Troubleshooting

**SSH refuses the connection or reports an authentication error.**
Verify access with `ssh user@host`. Load the key with `ssh-add` or pass its path
in `identity`. Add `"-v"` to `ssh_options` and use inherited stderr for diagnostics.

**The remote Python executable is missing.**
Pass the remote interpreter's name or absolute path in `python`.

**The connection drops before the handshake.**
Check remote interpreter errors and shell startup output. Startup scripts must
not write into the protocol's stdin/stdout channel.

**A custom return type cannot be unpickled.**
Define the type in the same importable module as its Tool and keep local client
setup separate. See {ref}`returning-custom-types` in {doc}`writing-tools`.

See {doc}`concepts` for resource ownership and cancellation, and
{doc}`api/sync` for the complete synchronous API.
