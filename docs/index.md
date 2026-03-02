# rmote

[![PyPI](https://img.shields.io/pypi/v/rmote?style=flat-square)](https://pypi.org/project/rmote)
![Python 3.11+](https://img.shields.io/pypi/pyversions/rmote?style=flat-square)
![remote deps: none](https://img.shields.io/badge/remote_deps-none-brightgreen?style=flat-square)
![typed: mypy strict](https://img.shields.io/badge/typed-mypy%20strict-blue?style=flat-square)
[![GitHub](https://img.shields.io/badge/GitHub-mosquito%2Frmote-181717?style=flat-square&logo=github)](https://github.com/mosquito/rmote)

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

| Tool                 | Remote requires          | Execution model                   | Native async | Connection  |
|----------------------|--------------------------|-----------------------------------|--------------|-------------|
| **Fabric**           | SSH + shell              | Shell commands only               | No           | per-call    |
| **Ansible**          | Python 2.6+ + modules    | Module push (JSON results)        | No           | per-run     |
| **RPyC zero-deploy** | Python + Plumbum locally | True RPC, transparent proxies     | Partial      | persistent  |
| **Mitogen**          | Python only              | Compressed bootstrap, RPC         | No           | per-run     |
| **rmote**            | Python stdlib only       | Compressed bootstrap, asyncio RPC | Yes          | persistent  |

The closest in spirit is **Mitogen** - it uses the same stdin-injection technique. However,
Mitogen predates Python 3's async model, is no longer actively maintained, and exposes a
lower-level channel API. `rmote` is built for asyncio from the ground up: concurrent multi-host
calls are first-class, tool definitions are plain Python classes, and return values are typed
dataclasses rather than JSON blobs.

Because `Protocol` is an ordinary async context manager, nothing prevents it from staying open
for hours or days. Bootstrap pays its cost once; every subsequent call travels over the
already-warmed channel. This makes long-running daemons a natural fit — see
{ref}`persistent-daemon` for a worked example.

## How It Works

```{mermaid}
graph LR
    subgraph local ["Local (your machine)"]
        hlp["Protocol<br/>+ _tools_cache"]
        tc["Tool classes<br/>(source extracted by ToolMeta)"]
    end

    subgraph remote ["Remote (injected process)"]
        rp["Protocol<br/>(asyncio event loop)"]
        ti["Tool instances<br/>(exec'd from source)"]
    end

    hlp <-->|"stdin / stdout<br/>packets"| rp
    tc -.->|"SYNC packet<br/>(first call only)"| ti
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
pip install rmote
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

**Version: 0.2.0** — Beta. Semver: patch releases fix bugs, minor releases add tools or protocol
features, major releases indicate breaking wire or API changes.

### Tests

The project ships an extensive test suite across three layers:

**Protocol layer** (`tests/` — 7 files):

- `test_protocol.py` — tool serialization round-trips, sync and async RPC calls, response
  matching by `packet_id`.
- `test_protocol_advanced.py` — concurrent in-flight requests, multi-tool sessions, tool
  inheritance.
- `test_protocol_errors.py` — remote exception propagation, original exception type preservation,
  tracebacks.
- `test_protocol_lowlevel.py` — raw packet encoding/decoding, LZMA compression threshold, flags
  bitmask, header magic.
- `test_stdio_protection.py` — verifies that spawning a subprocess inside a tool never touches
  the protocol pipes.
- `test_template.py` — template variable interpolation, control flow, `%%` / `##` / `\${`
  escapes, `render_template` helper.
- `test_tool_metaclass.py` — `ToolMeta` AST import extraction, class variable collection,
  `__init__` prohibition.

**Tool layer** (`tests/tools/` — 11 files, all run against live processes):

- `test_fs.py` — `FileSystem` unit tests (local) plus remote round-trips via the `protocol`
  fixture.
- `test_exec.py`, `test_logger.py`, `test_service.py`, `test_user.py` — built-in tools exercised
  against a local subprocess.
- `test_apt.py`, `test_apt_repository.py` — `Apt` and `AptRepository` tested inside a
  `python:3-slim` Docker container (install, remove, idempotency, TTL-aware update).
- `test_pacman.py`, `test_pacman_repository.py` — `Pacman` and `PacmanRepository` tested inside
  a locally-built `archlinux:python` Docker image.
- `test_template.py` — `Template` tool end-to-end over the RPC channel.
- `test_integration.py` — cross-tool interactions: concurrent `FileSystem` reads, mixed
  built-in + custom tool calls in a single session, error propagation through `asyncio.gather`.

**Tool fixtures** (`tests/tools_cases/` — 12 Tool subclass definitions):

Reusable fixtures shared across test files, each targeting a specific serialization or type
scenario: async methods, class-level constants, dataclass returns, nested dataclasses, enums
defined inside and outside the class, JSON-serializable types, math operations, module-level
imports, tool inheritance, user-lookup patterns.

**Documentation examples** — `pytest` discovers `README.md` and all files under `docs/` as test
sources. Every named code block is executed by `markdown-pytest` so all published snippets are
verified on each commit.

### Docker transport

The test suite already connects to Python interpreters running inside Docker containers using the
existing `from_subprocess` transport — `docker run --rm -i <image> python3 -qui` is just another
subprocess whose stdin/stdout carry the protocol. A dedicated `Protocol.from_docker` convenience
method is a natural next step.

### Type Safety

The entire `rmote/` package is type-checked under strict mypy. All public APIs carry full type
annotations and return values from RPC calls are typed dataclasses. Supported and tested on
Python 3.11, 3.12, 3.13, and 3.14.

### What is stable

Core protocol, SSH transport, subprocess transport, all 11 built-in tools, templating engine,
concurrent multi-host fan-out, Docker-based testing infrastructure.

### Not yet supported

Windows remote hosts, raw socket / TLS transports, `Protocol.from_docker` public API, streaming
or generator responses.

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
