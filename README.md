# rmote

![rmote](https://raw.githubusercontent.com/mosquito/rmote/master/docs/_static/logo.svg)

[![PyPI Version](https://img.shields.io/pypi/v/rmote.svg)](https://pypi.org/project/rmote/)
[![Python Versions](https://img.shields.io/pypi/pyversions/rmote.svg)](https://pypi.org/project/rmote/)
[![Tests](https://github.com/mosquito/rmote/actions/workflows/tests.yml/badge.svg)](https://github.com/mosquito/rmote/actions/workflows/tests.yml)
[![Docs](https://readthedocs.org/projects/rmote/badge/?version=latest)](https://rmote.readthedocs.io)
[![License](https://img.shields.io/pypi/l/rmote.svg)](https://github.com/mosquito/rmote/blob/master/LICENSE)

**Control any remote host. No agent. No install. Just a bare interpreter.**

---

## Installation

```bash
pip install rmote
```

Python 3.11+ is required on the **local** side. The remote needs only a standard Python 3 interpreter.

## Quick Start

Use `Connection` for synchronous scripts and `Protocol` for async applications.
Both clients call the same `def` and `async def` Tool methods.

### Synchronous local process

<!-- name: test_sync_quickstart; fixtures: client_resources; marks: timeout(15) -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory
from rmote.sync import Connection
from rmote.tools import FileSystem

with TemporaryDirectory() as directory:
    path = Path(directory) / "sample.txt"
    path.write_text("hello\n", encoding="utf-8")
    with Connection.from_local(rpc_timeout=5.0) as remote:
        assert remote(FileSystem.read_str, str(path)) == "hello\n"
        assert remote.call_with_timeout(2.0, FileSystem.read_str, str(path)) == "hello\n"
```

The factory completes the handshake before returning. Keep the connection open
for repeated calls. The context closes its subprocess and background thread,
including when an exception leaves the block.

### Asynchronous local process

<!-- name: test_quickstart; fixtures: client_resources; marks: timeout(15) -->
```python
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from rmote.protocol import Protocol
from rmote.tools import FileSystem


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
                assert await remote(FileSystem.read_str, str(path)) == "hello\n"
                assert await remote(FileSystem.read_str, str(path)) == "hello\n"
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()


asyncio.run(main())
```

The caller owns the process passed to `from_subprocess` and must reap it.
`Protocol.from_ssh` owns its SSH subprocess.

### SSH

For a synchronous script:

<!-- name: test_ssh_sync_quickstart; fixtures: docs_ssh; marks: timeout(20) -->
```python
from rmote.sync import Connection
from rmote.tools import FileSystem

with Connection.from_ssh("user@server", rpc_timeout=10.0) as remote:
    print(remote(FileSystem.glob, "/", "*"))
```

For an async application:

<!-- name: test_ssh_quickstart; fixtures: docs_ssh; marks: timeout(20) -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main() -> None:
    async with await Protocol.from_ssh("user@server") as remote:
        print(await remote(FileSystem.glob, "/", "*"))


asyncio.run(main())
```

Both factories accept `user`, `port`, `identity`, `python`, `ssh_options`, and
`stderr`. The synchronous factory also accepts `connect_timeout`, `rpc_timeout`,
and `close_timeout`. See the [quickstart](https://rmote.readthedocs.io/en/latest/quickstart.html)
for SSH options, jump hosts, errors, and explicit cleanup.

## Deadlines and Lifecycle

`Connection.from_local()` and `Connection.from_ssh(...)` each own one subprocess
and one private background event loop thread. Use `with` or `try/finally` with
`close()`. Repeated `close()` is safe; calls after closing and nested context
entry raise `RuntimeError`.

`connect_timeout=30.0` limits startup and handshake. `rpc_timeout=None` sets the
default for calls. `call_with_timeout(timeout, tool, /, *args, **kwargs)` overrides
one call. Each RPC deadline includes the first Tool upload, send, and response.
All keyword arguments belong to the remote method.

Timeout raises `TimeoutError`. Timeout and `KeyboardInterrupt` stop local
waiting; remote work can continue. Cancellation during packet transmission can
break the channel. Close a failed connection and create another.

`close_timeout=5.0` limits graceful shutdown before terminate, a one-second grace
period, and kill. It does not bound total cleanup time: local cancellation and
executor shutdown require cooperative code. Numeric deadlines must be finite
and positive; only connect and RPC deadlines accept `None`.

Use `Protocol` in async code, or put the complete synchronous lifecycle inside
`asyncio.to_thread`. Direct synchronous calls block the caller's event loop.
Remote log records invoke local handlers in the background loop thread. Handlers
must be thread safe and must not call that connection's synchronous methods.

## How It Works

Three steps happen on every connection:

1. **Bootstrap** — `protocol.py` is lzma-compressed, base64-encoded (~few KB), and written to the remote's stdin. 
   The remote executes it and writes `PROTOCOL READY`.
2. **Tool sync** — On first use, the tool class source is sent to the remote as a SYNC packet. The remote `exec()`s 
   it into a fresh module. Subsequent calls over the same connection skip this step.
3. **RPC** — Each `await proto(Tool.method, *args)` gets a unique `packet_id`. The remote runs the method
   (sync or async) and returns the result. Multiple in-flight calls execute concurrently on both sides.

```
Local                          Remote (injected process)
──────                         ──────────────────────────
await from_subprocess()  ───►  exec(decompress(b64decode(payload)))
                         ◄───  PROTOCOL READY

await proto(Tool.method) ───►  SYNC tool source
                         ◄───  ACK

await proto(Tool.method) ───►  REQUEST {method, args, id=1}
                         ◄───  RESPONSE {result, id=1}
```

## Writing Custom Tools

A Tool is a Python class whose methods run in the remote interpreter. Keep its
definition and return types in an importable module. Put connections and local
setup in a separate client script; the same Tool module works with both clients.

See [Writing Tools](https://rmote.readthedocs.io/en/latest/writing-tools.html)
for module and inline examples, custom return types, imports, and serialization.
Runnable clients are in [examples/local_sync.py](examples/local_sync.py) and
[examples/local_async.py](examples/local_async.py), with their shared Tool in
[examples/lifecycle_tools.py](examples/lifecycle_tools.py).

- Inherit from `Tool`; use static or class methods.
- Do not define `__init__`; the metaclass raises `TypeError`.
- Use `def` for blocking remote work and `async def` for awaitable work.
- Remote imports must be available on the remote host. Built-in tools use the standard library.
- Return serializable values and define custom types in the Tool module.
- Use `process()` for subprocesses so children do not share protocol pipes.

### Running Subprocesses

The remote Python process communicates with the local side over its own **stdin / stdout** as a
binary packet stream. Any child process that inherits the default file descriptors will share
those pipes — anything the child writes to stdout will corrupt the packet framing and break the
connection permanently.

Use `process` from `rmote.protocol` instead. It always redirects `stdin`, `stdout`, and `stderr`
away from the protocol pipes:

<!-- name: test_process -->
```python
from rmote.protocol import Tool, process


class DeployTool(Tool):
    @staticmethod
    def apt_update() -> int:
        """Update package lists; return exit code."""
        result = process("apt-get", "update")
        return result.returncode

    @staticmethod
    def git_log(repo: str) -> str:
        """Return the last commit message."""
        result = process("git", "-C", repo, "log", "-1", "--oneline",
                         capture_output=True, text=True, check=True)
        return result.stdout.strip()
```

`process` is available in every tool namespace without an import for inline tools. For
file-level tools add `from rmote.protocol import process` at the top of the file.

| Default                                         | Why                                    |
|-------------------------------------------------|----------------------------------------|
| `stdin=DEVNULL`                                 | Child cannot consume protocol data     |
| `stdout=DEVNULL` (unless `capture_output=True`) | Child cannot corrupt the packet stream |
| `stderr=DEVNULL` (unless `capture_output=True`) | Remote stderr is the same channel      |

<!-- name: test_subprocess_example -->
```python
import subprocess
from rmote.protocol import process

if __name__ == "__main__":
    # BAD — child inherits the protocol pipes
    subprocess.run(["apt-get", "update"])

    # GOOD
    process("apt-get", "update")
```

## Concurrent Calls

An open synchronous connection can accept calls from multiple caller threads:

<!-- name: test_sync_concurrent; fixtures: client_resources; marks: timeout(15) -->
```python
from concurrent.futures import ThreadPoolExecutor
from rmote.sync import Connection
from rmote.tools import FileSystem

with Connection.from_local(rpc_timeout=5.0) as remote:
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(remote, FileSystem.glob, "/", "*") for _ in range(2)]
        results = [call.result() for call in calls]
        assert all(isinstance(result, list) for result in results)
```

Keep the connection open until its caller threads finish. Each connection owns
its private loop; a shared runtime is not available.

Multiple RPC calls execute concurrently over the same connection via `asyncio.gather`:

<!-- name: test_concurrent; fixtures: client_resources; marks: timeout(15) -->
```python
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from rmote.protocol import Protocol
from rmote.tools import FileSystem


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
                contents = await asyncio.gather(
                    remote(FileSystem.read_str, str(path)),
                    remote(FileSystem.read_str, str(path)),
                )
                assert contents == ["hello\n", "hello\n"]
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()


asyncio.run(main())
```

Both calls are dispatched immediately — the channel does not wait for the first response before sending the second. 
Each response is matched back to its caller by `packet_id`.

## Multi-Host Fan-Out

Fan out to several SSH hosts in parallel with `asyncio.gather`:

<!-- name: test_fanout -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


async def read_hostname(host: str) -> str:
    async with await Protocol.from_ssh(host) as proto:
        return await proto(FileSystem.read_str, "/etc/hostname")


async def main() -> None:
    names = await asyncio.gather(*[read_hostname(h) for h in HOSTS])
    for host, name in zip(HOSTS, names):
        print(f"{host}: {name.strip()}")


if __name__ == "__main__":
    asyncio.run(main())
```

Pass `return_exceptions=True` so a failure on one host does not cancel the others:

<!-- name: test_fanout_errors -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "broken-host"]


async def read_hostname(host: str) -> str:
    async with await Protocol.from_ssh(host) as proto:
        return await proto(FileSystem.read_str, "/etc/hostname")


async def main() -> None:
    results = await asyncio.gather(
        *[read_hostname(h) for h in HOSTS],
        return_exceptions=True,
    )
    for host, result in zip(HOSTS, results):
        if isinstance(result, BaseException):
            print(f"{host}: ERROR — {result}")
        else:
            print(f"{host}: {result.strip()}")


if __name__ == "__main__":
    asyncio.run(main())
```

To keep connections open across multiple rounds, use `AsyncExitStack`:

<!-- name: test_persistent -->
```python
import asyncio
from contextlib import AsyncExitStack
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


async def main() -> None:
    async with AsyncExitStack() as stack:
        protos = [
            await stack.enter_async_context(await Protocol.from_ssh(host))
            for host in HOSTS
        ]

        # Round 1 — read hostnames (FileSystem synced once per connection)
        names = await asyncio.gather(*[
            p(FileSystem.read_str, "/etc/hostname") for p in protos
        ])
        print("hostnames:", [n.strip() for n in names])

        # Round 2 — list log files (no re-sync needed)
        logs = await asyncio.gather(*[
            p(FileSystem.glob, "/var/log", "*.log") for p in protos
        ])
        for host, filelist in zip(HOSTS, logs):
            print(f"{host}: {len(filelist)} log files")


if __name__ == "__main__":
    asyncio.run(main())
```

## Templating

rmote includes a minimal template engine — no Jinja2 needed on the remote side.

### Variable interpolation

Wrap any Python expression in `${…}`:

<!-- name: test_template_interpolation -->
```python
from rmote.protocol import Template

assert Template("Hello, ${name}!").render(name="Alice") == "Hello, Alice!"
assert Template("${', '.join(sorted(d.keys()))}").render(d={"b": 2, "a": 1}) == "a, b"
```

### Control flow

Lines starting with `%` are Python control flow. `endfor` / `endif` / `end` close blocks:

<!-- name: test_template_control -->
```python
from rmote.protocol import Template

tmpl = Template("""\
% for item in items:
- ${item}
% endfor""")
assert tmpl.render(items=["alpha", "beta", "gamma"]) == "- alpha\n- beta\n- gamma"

tmpl = Template("""\
% if n > 0:
positive
% elif n == 0:
zero
% else:
negative
% endif""")
assert tmpl.render(n=1) == "positive"
assert tmpl.render(n=0) == "zero"
assert tmpl.render(n=-1) == "negative"
```

Lines starting with `##` are stripped; `%%` emits a literal `%`; `\${` escapes interpolation:

<!-- name: test_template_special -->
```python
from rmote.protocol import Template

assert Template("## ignored\nresult: ${v}").render(v=42) == "result: 42"
assert Template("%% done ${n}/10").render(n=7) == "% done 7/10"
assert Template(r"\${not_a_var}").render() == "${not_a_var}"
```

### Pickling

`Template` instances are picklable — compile locally, pass as an argument to a remote tool call, and render on the 
remote with host-specific variables:

<!-- name: test_template_pickle -->
```python
import pickle
from rmote.protocol import Template

tmpl = Template("server_name ${hostname}; listen ${port};")
restored = pickle.loads(pickle.dumps(tmpl))
assert restored.render(hostname="example.com", port=443) == "server_name example.com; listen 443;"
```

### `render_template`

`render_template` compiles and renders in one step:

<!-- name: test_render_template -->
```python
from rmote.protocol import render_template

result = render_template(
    "Hi ${name}, you have ${count} message${'s' if count != 1 else ''}.",
    name="Bob",
    count=3,
)
assert result == "Hi Bob, you have 3 messages."
```

## Error Handling

Exceptions raised on the remote side are re-raised locally with the original type:

<!-- name: test_errors; fixtures: client_resources; marks: timeout(15) -->
```python
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main() -> None:
    with TemporaryDirectory() as directory:
        missing = str(Path(directory) / "missing.txt")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-qui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with await Protocol.from_subprocess(process) as remote:
                try:
                    await remote(FileSystem.read_str, missing)
                except FileNotFoundError as error:
                    print(f"caught: {error}")
                else:
                    raise AssertionError("The missing file must raise FileNotFoundError")
                assert await remote(FileSystem.glob, directory, "*") == []
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()


asyncio.run(main())
```

## SSH Options

All common SSH options are available as keyword arguments to `from_ssh`:

<!-- name: test_ssh_options; fixtures: docs_ssh; marks: timeout(20) -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main() -> None:
    async with await Protocol.from_ssh(
        "myserver",
        user="deploy",
        port=2222,
        identity="/home/deploy/.ssh/id_ed25519",
        python="python3.11",
        ssh_options=["-o", "BatchMode=yes"],
    ) as remote:
        files = await remote(FileSystem.glob, "/var/log", "*.log")
        assert isinstance(files, list)
        print(files)


asyncio.run(main())
```

## Built-in Tools

All built-in tools use only the Python stdlib on the remote side.

| Tool               | What it manages                              | Platform        |
|--------------------|----------------------------------------------|-----------------|
| `FileSystem`       | Read, write, glob, idempotent line-in-file   | any             |
| `Exec`             | Run commands and shell expressions           | any             |
| `Logger`           | Remote log level and log record forwarding   | any             |
| `Template`         | Mako-like template rendering on the remote   | any             |
| `Quit`             | Cleanly exit the remote process              | any             |
| `Service`          | systemd units — start, stop, enable, disable | Linux           |
| `User`             | Create and manage users and groups           | Linux           |
| `Apt`              | Install and remove packages                  | Debian / Ubuntu |
| `AptRepository`    | DEB822 sources and GPG keys                  | Debian / Ubuntu |
| `Pacman`           | Install and remove packages                  | Arch Linux      |
| `PacmanRepository` | Repository sections and GPG keys             | Arch Linux      |

## Comparison

| Tool                 | Remote requires          | Native async  | Execution model                                  |
|----------------------|--------------------------|:-------------:|--------------------------------------------------|
| **Fabric**           | SSH + shell              |       —       | Shell commands only                              |
| **Ansible**          | Python 2.6+ + modules    |       —       | Module push (JSON results)                       |
| **Mitogen**          | Python only              |       —       | Compressed bootstrap, lower-level channel API    |
| **RPyC zero-deploy** | Python + Plumbum locally |    partial    | Transparent object proxies                       |
| **rmote**            | Python stdlib only       |    **yes**    | Compressed bootstrap, asyncio RPC, typed returns |

rmote is closest in spirit to Mitogen — same stdin-injection technique — but is built for asyncio from the ground up.
Concurrent multi-host calls are first-class, tools are plain Python classes, and return values are typed dataclasses
rather than JSON blobs.

## Project Status

**Beta.** Semver: patch = bug fix, minor = new tool or protocol feature,
major = breaking wire or API change.

### Tests

The test suite covers three layers:

**Protocol** — tool serialization, sync and async RPC round-trips, concurrent
in-flight requests matched by `packet_id`, remote exception propagation with original type
preservation, raw packet encoding/decoding, LZMA compression threshold, and a dedicated test
that verifies spawning a subprocess inside a tool never corrupts the protocol pipes.

**Tools** (integration tests run against live processes):

- `FileSystem`, `Exec`, `Logger`, `Service`, `User`, `Template` — tested against a local
  subprocess.
- `Apt` and `AptRepository` — tested inside a `python:3-slim` Docker container: install,
  remove, idempotency checks, TTL-aware `update`.
- `Pacman` and `PacmanRepository` — tested inside a locally-built `archlinux:python` Docker
  image.
- Cross-tool integration: concurrent reads, mixed built-in and custom tools in a single session,
  error propagation through `asyncio.gather`.

**Reusable `Tool` fixtures** in `tests/tools_cases/` cover the serialization corner cases:
async methods, class-level constants, dataclass and nested-dataclass returns, enums defined
inside and outside the class, module-level imports, tool inheritance, and same-name tools in
different modules (name collision safety).

**README and docs examples** — `pytest` treats `README.md` and all files under `docs/` as test
sources. Named code blocks are collected by `markdown-pytest`. Local client examples execute
real RPCs. SSH documentation examples use an isolated local SSH server when available.

### Docker transport

The test suite already runs Python interpreters inside Docker containers using `from_subprocess`
(`docker run --rm -i <image> python3 -qui` is just another subprocess). A dedicated
`Protocol.from_docker` convenience method is a natural next step.

### Type Safety

The entire `rmote/` package passes strict mypy with no errors. All public APIs are fully
annotated. Tested on Python 3.11, 3.12, 3.13, and 3.14.

### Stable

Core protocol, SSH transport, subprocess transport, all 11 built-in tools, templating engine,
concurrent multi-host fan-out, Docker-based test infrastructure.

### Not yet supported

Windows remote hosts, raw socket / TLS transports, `Protocol.from_docker` public API, streaming
or generator responses.
