# rmote

[![PyPI](https://img.shields.io/pypi/v/rmote?style=flat-square)](https://pypi.org/project/rmote)
![Python 3.11+](https://img.shields.io/pypi/pyversions/rmote?style=flat-square)
![remote deps: none](https://img.shields.io/badge/remote_deps-none-brightgreen?style=flat-square)
![typed: mypy strict](https://img.shields.io/badge/typed-mypy%20strict-blue?style=flat-square)

**Asyncio RPC for remote Python processes that have nothing installed.**

## Motivation

Many remote hosts - fresh VMs, CI runners, minimal containers, embedded devices - have Python
available but nothing else: no virtualenv, no pip, no pre-installed agent. The usual answer is
"install the installer first", which just pushes the bootstrapping problem one step earlier.

`rmote` takes a different approach: it compresses and base64-encodes its entire runtime (a few KB),
writes it to the remote's stdin, and has the remote interpreter execute it directly. The remote
process needs only the Python standard library.

Once connected, you interact with the remote host by writing ordinary Python classes - not YAML
files, not shell scripts, not JSON-RPC stubs. Methods are async-native, return values are typed
dataclasses, and multiple in-flight calls execute concurrently on both sides.

## Prior Art

| Tool                 | Remote requires          | Execution model                   | Native async |
|----------------------|--------------------------|-----------------------------------|--------------|
| **Fabric**           | SSH + shell              | Shell commands only               | No           |
| **Ansible**          | Python 2.6+ + modules    | Module push (JSON results)        | No           |
| **RPyC zero-deploy** | Python + Plumbum locally | True RPC, transparent proxies     | Partial      |
| **Mitogen**          | Python only              | Compressed bootstrap, RPC         | No           |
| **rmote**            | Python stdlib only       | Compressed bootstrap, asyncio RPC | Yes          |

The closest in spirit is **Mitogen** - it uses the same stdin-injection technique. However,
Mitogen predates Python 3's async model, is no longer actively maintained, and exposes a
lower-level channel API. `rmote` is built for asyncio from the ground up: concurrent multi-host
calls are first-class, tool definitions are plain Python classes, and return values are typed
dataclasses rather than JSON blobs.

## How It Works

```{mermaid}
graph LR
    subgraph local ["Local (your machine)"]
        hlp["Protocol\n+ _tools_cache"]
        tc["Tool classes\n(source extracted by ToolMeta)"]
    end

    subgraph remote ["Remote (injected process)"]
        rp["Protocol\n(asyncio event loop)"]
        ti["Tool instances\n(exec'd from source)"]
    end

    hlp <-->|"stdin / stdout\npackets"| rp
    tc -.->|"SYNC packet\n(first call only)"| ti
```

Full detail is in {doc}`concepts`, but the three-step summary:

1. **Bootstrap** - `protocol.py` is compressed, base64-encoded, and written to the remote's
   stdin. The remote interpreter executes it and writes `PROTOCOL READY` to signal that the
   channel is open.

2. **Tool sync** - On first use, the tool's source code is extracted locally via `inspect` and
   sent as a SYNC packet. The remote `exec()`s it into a fresh module. Subsequent calls skip
   this step entirely.

3. **RPC** - Each `await proto(Tool.method, *args)` gets a unique `packet_id`. The remote runs
   the method (sync or async) and returns the result through the same channel. Multiple in-flight
   calls execute concurrently on both sides via asyncio task spawning.

```{mermaid}
sequenceDiagram
    participant L as Local
    participant R as Remote

    Note over L,R: Bootstrap
    L->>R: exec(decompress(b64decode(payload)))
    R-->>L: PROTOCOL READY

    Note over L,R: Tool sync (first call only)
    L->>R: SYNC tool_source
    R-->>L: ACK

    Note over L,R: RPC
    L->>R: REQUEST {method, args, packet_id}
    R-->>L: RESPONSE {result, packet_id}
```

## Quick Example

<!-- name: test_quick_example -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools.fs import FileSystem

async def main():
    async with await Protocol.from_ssh("web01.example.com") as proto:
        hosts_content, uptime = await asyncio.gather(
            proto(FileSystem.read_str, "/etc/hosts"),
            proto(FileSystem.read_str, "/proc/uptime"),
        )
    print(hosts_content)
    print(uptime)

if __name__ == "__main__":
    asyncio.run(main())
```

Both calls are dispatched immediately - the channel does not wait for the first response before
sending the second. The local event loop matches each response back to its caller by `packet_id`:

```{mermaid}
sequenceDiagram
    participant L as Local
    participant R as Remote

    L->>R: REQUEST {FileSystem.read, "/etc/hosts",  id=1}
    L->>R: REQUEST {FileSystem.read, "/proc/uptime", id=2}
    R-->>L: RESPONSE {hosts_content, id=1}
    R-->>L: RESPONSE {uptime,        id=2}
```

Install on the local side only - the remote needs nothing:

```bash
pip install `rmote`
```

## Built-in Tools

| Tool               | Manages                                      | Platform        |
|--------------------|----------------------------------------------|-----------------|
| `FileSystem`       | Read, write, glob, idempotent line-in-file   | any             |
| `Exec`             | Run commands and shell expressions           | any             |
| `Service`          | systemd units (start/stop/enable/disable)    | Linux           |
| `User`             | Users and groups                             | Linux           |
| `Apt`              | Packages and package state                   | Debian / Ubuntu |
| `AptRepository`    | DEB822 sources and GPG keys                  | Debian / Ubuntu |
| `Pacman`           | Packages and package state                   | Arch Linux      |
| `PacmanRepository` | Repository sections and GPG keys             | Arch Linux      |
| `Logger`           | Remote log level and forwarding              | any             |
| `Template`         | Mako-like template rendering                 | any             |
| `Quit`             | Remote process exit                          | any             |

## Project Status

**Version: 0.3.0** - semver: patch releases fix bugs, minor releases add tools or protocol
features, major releases indicate breaking wire or API changes.

**Stable:** core protocol, SSH transport, all 11 built-in tools, templating engine. The entire
``rmote`/` package is type-checked with strict mypy.

**Not yet supported:** Windows remote hosts, non-SSH transports (socket, docker exec), streaming
or generator responses, TLS-encrypted channels.

```{toctree}
:maxdepth: 2
:caption: User Guide

quickstart
multi-host
concepts
writing-tools
templating
```

```{toctree}
:maxdepth: 2
:caption: API Reference

api/index
```
