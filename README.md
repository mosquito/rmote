# rmote

![rmote](docs/_static/logo.svg)

**Control any remote host. No agent. No install. Just a bare interpreter.**

---

## Installation

```bash
pip install rmote
```

Python 3.11+ is required on the **local** side. The remote needs only a standard Python 3 interpreter.

## Quick Start

Connect to a local subprocess and call remote methods:

<!-- name: test_quickstart -->
```python
import asyncio
import sys
from rmote.protocol import Protocol
from rmote.tools.fs import FileSystem


async def main():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    async with await Protocol.from_subprocess(process) as proto:
        content = await proto(FileSystem.read_str, "/etc/hostname")
        print(content)


if __name__ == "__main__":
    asyncio.run(main())
```

For SSH, replace `from_subprocess` with `from_ssh`:

<!-- name: test_ssh_quickstart -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    async with await Protocol.from_ssh("user@server") as proto:
        content = await proto(FileSystem.read_str, "/etc/hostname")
        print(content)


if __name__ == "__main__":
    asyncio.run(main())
```

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

A **Tool** is a Python class whose methods execute on the remote side. Define it locally; the source is transferred 
automatically on first use.

<!-- name: test_custom_tool -->
```python
import dataclasses
from rmote.protocol import Tool


@dataclasses.dataclass
class DiskInfo:
    path: str
    free: int


class SystemTool(Tool):
    @staticmethod
    def hostname() -> str:
        import socket
        return socket.gethostname()

    @staticmethod
    def disk_free(path: str = "/") -> "DiskInfo":
        import shutil
        _, _, free = shutil.disk_usage(path)
        return DiskInfo(path=path, free=free)

    @staticmethod
    async def read(path: str) -> str:
        with open(path) as f:
            return f.read()
```

<!-- name: test_custom_tool -->
```python
import asyncio
import sys
from rmote.protocol import Protocol


async def main():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    async with await Protocol.from_subprocess(process) as proto:
        host = await proto(SystemTool.hostname)
        info = await proto(SystemTool.disk_free, "/")
        print(f"{host}: {info.free // 2**30} GB free on /")


if __name__ == "__main__":
    asyncio.run(main())
```

**Rules:**

- Inherit from `Tool`
- No `__init__` — the metaclass raises `TypeError` if defined
- Static or class methods only — no instance state across calls
- Stdlib imports only — put `import` statements inside the method body so they run on the remote
- Any picklable value can be returned, including dataclasses
- **Use `process()` for subprocesses** — never `subprocess.run` or `os.system` directly (see below)

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

Multiple RPC calls execute concurrently over the same connection via `asyncio.gather`:

<!-- name: test_concurrent -->
```python
import asyncio
import sys
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    async with await Protocol.from_subprocess(process) as proto:
        hosts, uptime = await asyncio.gather(
            proto(FileSystem.read_str, "/etc/hosts"),
            proto(FileSystem.read_str, "/etc/hostname"),
        )
        print(hosts[:50])
        print(uptime.strip())


if __name__ == "__main__":
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
        protos = await asyncio.gather(*[
            stack.enter_async_context(await Protocol.from_ssh(h))
            for h in HOSTS
        ])

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

<!-- name: test_errors -->
```python
import asyncio
import sys
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    async with await Protocol.from_subprocess(process) as proto:
        try:
            await proto(FileSystem.read_str, "/nonexistent/path/file.txt")
        except FileNotFoundError as e:
            print(f"caught: {e}")


if __name__ == "__main__":
    asyncio.run(main())
```

## SSH Options

All common SSH options are available as keyword arguments to `from_ssh`:

<!-- name: test_ssh_options -->
```python
from rmote.protocol import Protocol


async def main():
    proto = await Protocol.from_ssh(
        "myserver",
        user="deploy",
        port=2222,
        identity="~/.ssh/id_ed25519",
        python="python3.11",
        ssh_options=["-o", "StrictHostKeyChecking=no"],
    )


if __name__ == "__main__":
    import asyncio
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
