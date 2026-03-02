# Quickstart

## Installation

```bash
pip install rmote
```

rmote requires Python 3.11 or newer on the **local** side.
The remote side needs only a standard Python 3 interpreter - no extra packages.

## Local Subprocess

The simplest usage is to spawn a local Python subprocess and communicate with it:

<!-- name: test_local_subprocess -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools.fs import FileSystem


async def main():
    process = await asyncio.create_subprocess_exec(
        "python3", "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    proto = await Protocol.from_subprocess(process)

    async with proto:
        files = await proto(FileSystem.glob, "/etc/", "*.conf")
        print(files)

        content = await proto(FileSystem.read_str, "/etc/hostname")
        print(content)


if __name__ == "__main__":
    asyncio.run(main())
```

The `-qui` flags run Python in quiet, unbuffered, isolated mode - recommended for
subprocess communication to avoid banner text and buffering issues.

## SSH Remote Process

Use {meth}`~rmote.protocol.BaseProtocol.from_ssh` to connect to a remote host.
The SSH subprocess is managed automatically - it is terminated when the context exits.

<!-- name: test_ssh_remote -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    async with await Protocol.from_ssh("user@server") as proto:
        logs = await proto(FileSystem.glob, "/var/log/", "*.log")
        print(logs)


if __name__ == "__main__":
    asyncio.run(main())
```

All common SSH options are supported as keyword arguments:

<!-- name: test_ssh_options -->
```python
import asyncio
from rmote.protocol import Protocol


async def main():
    async with await Protocol.from_ssh(
        "myserver",
        user="deploy",
        port=2222,
        identity="~/.ssh/id_ed25519",
        python="python3.11",
        ssh_options=["-o", "StrictHostKeyChecking=no"],
    ) as proto:
        pass  # use proto here


if __name__ == "__main__":
    asyncio.run(main())
```

## Jump Hosts

Pass `-J` via `ssh_options` to route the connection through one or more bastion hosts.

### Single jump host

<!-- name: test_jump_single -->
```python
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    async with await Protocol.from_ssh(
        "internal-host",
        ssh_options=["-J", "bastion.example.com"],
    ) as proto:
        hostname = await proto(FileSystem.read_str, "/etc/hostname")
        print(hostname)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

SSH opens a connection to `bastion.example.com` first, then tunnels through it to
`internal-host`. The `Protocol` context sees only the final destination.

### Two jump hosts

Chain multiple bastions as a comma-separated list:

<!-- name: test_jump_double -->
```python
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    async with await Protocol.from_ssh(
        "deep-internal-host",
        ssh_options=["-J", "bastion1.example.com,bastion2.internal"],
    ) as proto:
        hostname = await proto(FileSystem.read_str, "/etc/hostname")
        print(hostname)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

SSH hops `local → bastion1 → bastion2 → deep-internal-host` transparently.
Each jump host can specify its own user and port with the usual `user@host:port` syntax:

<!-- name: test_jump_userport -->
```python
from rmote.protocol import Protocol


async def main():
    async with await Protocol.from_ssh(
        "10.0.2.5",
        ssh_options=["-J", "deploy@bastion1.example.com:2222,relay@bastion2.internal"],
    ) as proto:
        pass


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

For patterns that fan out to multiple hosts in parallel see {doc}`multi-host`.

## Concurrent Calls

Multiple RPC calls execute concurrently - use `asyncio.gather` to run them in parallel:

<!-- name: test_concurrent_calls -->
```python
import asyncio
from rmote.protocol import Protocol, Tool
from rmote.tools import FileSystem


class MyTool(Tool):
    @staticmethod
    def shout(text: str) -> str:
        return text.upper()


async def main():
    process = await asyncio.create_subprocess_exec(
        "python3", "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    proto = await Protocol.from_subprocess(process)

    async with proto:
        results = await asyncio.gather(
            proto(FileSystem.read_str, "/etc/hosts"),
            proto(FileSystem.read_str, "/etc/hostname"),
            proto(FileSystem.glob, "/tmp", "*.txt"),
            proto(MyTool.shout, "hello"),
        )
        print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

Each call gets a unique `packet_id`; responses are matched back to their callers
regardless of the order in which the remote side completes them.

## Troubleshooting

**`ssh: connect to host … port 22: Connection refused` / `Permission denied (publickey)`**
Your SSH key is not loaded or the remote host requires a different key.  Run `ssh-add` to
load your key into the agent, or pass `identity="~/.ssh/id_ed25519"` (or whichever key file
applies) to `from_ssh`.  Verify with `ssh user@host` before using rmote.

**`/usr/bin/env: 'python3': No such file or directory`**
The remote host does not have `python3` on the default `PATH`, or the binary has a different
name.  Pass `python="python3.12"` (or the full path such as `python="/usr/local/bin/python3"`)
to `from_ssh`.

**`BrokenPipeError` or the connection drops immediately**
The remote process exited before the protocol was established.  Common causes: the remote
shell prints a banner (disable with `-q` in `ssh_options`), the remote Python crashes during
bootstrap, or a `.bashrc` / `.profile` writes to stdout.  Add `-v` to `ssh_options` for
verbose SSH diagnostics.

**`_pickle.UnpicklingError` or `AttributeError` on the return value**
A custom type returned from a Tool method (e.g. a dataclass) must be defined *outside* the
Tool class body and importable in both the local and remote namespaces.  If it is defined
inside the tool source it will exist remotely but not locally, so unpickling will fail.
See {ref}`Returning Custom Types <returning-custom-types>` for the correct pattern.

Two distinct tool classes appear in the gather above - `FileSystem` and `MyTool`. Each is synced
exactly once, on its first call through this connection. The three `FileSystem` calls share a
single sync; `MyTool` gets its own. All subsequent calls over the same connection go straight to
RPC with no re-transfer.

## Error Handling

Exceptions raised on the remote side are serialized and re-raised locally:

<!-- name: test_error_handling -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem


async def main():
    process = await asyncio.create_subprocess_exec(
        "python3", "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    proto = await Protocol.from_subprocess(process)

    async with proto:
        try:
            content = await proto(FileSystem.read_str, "/nonexistent/file.txt")
        except FileNotFoundError as e:
            print(f"Remote error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
```
