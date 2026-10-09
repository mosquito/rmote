# Agent

`Agent` forwards an SSH agent through rmote between POSIX endpoints. It needs
an existing agent socket on the agent endpoint. rmote transfers its forwarding
code automatically; the listener endpoint needs no installed `rmote` package
or `sshd`. The program using the forwarded agent must be installed there.

## Choose the endpoints

Call `Agent.forward` directly as an async context manager. Do not pass it to
`remote(...)`. Each endpoint is an open async `Protocol`; `None` or an omitted
argument selects the current Python process. The synchronous `Connection`
cannot be used as an endpoint for this context manager.

| Call | Agent socket | New listener socket |
| --- | --- | --- |
| `Agent.forward(listener=remote)` | Local | Remote |
| `Agent.forward(agent=remote)` | Remote | Local |
| `Agent.forward(listener=host_a, agent=host_b)` | Host B | Host A |

`path=` selects an existing socket on the **agent endpoint**. If omitted,
the Tool reads `SSH_AUTH_SOCK` on that endpoint. The context yields the path
of a new socket on the **listener endpoint**. Set the consuming process's
`SSH_AUTH_SOCK` to this path. The context does not change process environments.

## Use a local agent remotely

Run this example with a local agent containing a key and `SSH_AUTH_SOCK` set.
It uses a local subprocess as the target so it runs without an SSH server.
Replace `Protocol.from_command(python=sys.executable)` with an appropriate
{doc}`transport <../../transports>` to reach another host.

<!-- name: test_agent_forward; fixtures: docs_ssh_agent -->
```python
import asyncio
import sys

from rmote.protocol import Protocol
from rmote.tools import Agent, Exec


async def main():
    async with await Protocol.from_command(python=sys.executable) as remote:
        async with Agent.forward(listener=remote) as socket_path:
            result = await remote(
                Exec.command, "ssh-add", "-L",
                env={"SSH_AUTH_SOCK": socket_path}, capture_output=True,
            )
            assert result.returncode == 0, result.stderr
            print(result.stdout.decode())


asyncio.run(main())
```

`ssh-add` runs on the listener endpoint and contacts the local agent through
the yielded socket. Pass `path="/path/to/local/agent.sock"` to select a known
socket instead of the local `SSH_AUTH_SOCK`.

## Use a remote agent locally

This example reverses the direction. The subprocess inherits `SSH_AUTH_SOCK`
for this local demonstration. With a remote transport, use the remote
environment or pass `path="/path/to/remote/agent.sock"` explicitly.

<!-- name: test_agent_reverse; fixtures: docs_ssh_agent -->
```python
import asyncio
import os
import sys

from rmote.protocol import Protocol
from rmote.tools import Agent


async def main():
    async with await Protocol.from_command(python=sys.executable) as remote:
        async with Agent.forward(agent=remote) as socket_path:
            process = await asyncio.create_subprocess_exec(
                "ssh-add", "-L",
                env=dict(os.environ, SSH_AUTH_SOCK=socket_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, error = await process.communicate()
            assert process.returncode == 0, error
            print(output.decode())


asyncio.run(main())
```

## Lifetime and errors

Keep the endpoint Protocols and the forwarding context open while consumers
use the socket. Bytes pass through the coordinator process; two remote
endpoints do not open a direct connection to each other. Exiting the context
closes both sessions and their connections and removes the listener socket
and its private temporary directory. Startup failure and cancellation also
trigger cleanup. Forcefully terminating an endpoint can leave its temporary
directory behind; its sockets can no longer forward requests.

Startup probes the source socket before creating the listener. An unset path
or an inaccessible socket raises `OSError`; connection attempts have a
five-second timeout. Unlike the context manager, the `sshmux` CLI handles an
unavailable agent by warning and continuing without forwarding.

Each endpoint session permits 32 connections. A listener uses a mode `0600`
socket inside a mode `0700` directory. The relay sends at most 64 KiB per
chunk and preserves half-close, so a final response can follow request EOF.

This is a byte relay. It adds no OpenSSH `session-bind@openssh.com` binding
for the rmote hop. Destination constraints and restrictions that depend on
recognizing a forwarded connection cannot validate this hop. See
{doc}`the sshmux guide <../../sshmux>` for CLI usage and this limitation.

## Endpoint operations

`Agent.forward` manages these operations for you. For a custom coordinator,
call them locally or through `Protocol`. Start a session on each endpoint
and pass its `session_id` to every operation. Connection IDs belong only to
their session; an unknown session or connection raises `ConnectionError`.

| Operation | Result or effect |
| --- | --- |
| `start(session_id=None)` | Create a session and return its ID |
| `listen(session_id)` | Create the session's listener and return its path |
| `accept(session_id)` | Stream accepted connection IDs; closing this stream releases the listener session |
| `connect(session_id, path=None, connection_id=None)` | Connect to an existing agent socket and return a connection ID |
| `output(session_id, connection_id)` | Stream incoming bytes; only one reader per connection |
| `write(session_id, connection_id, data)` | Write a chunk and wait for buffer drainage |
| `end_input(session_id, connection_id)` | Send EOF while allowing output to continue |
| `close(session_id, connection_id)` | Close one connection; safe to repeat |
| `release(session_id)` | Close and remove the session; safe to repeat |

Supply unique IDs before resource creation if cancellation can prevent you
from receiving a result. Wait for outstanding `start`, `listen`, and `connect`
calls before releasing their sessions: cancelling local waiting does not
cancel an ordinary remote RPC. Prefer `Agent.forward` for this coordination.

## API reference

```{eval-rst}
.. autoclass:: rmote.tools.agent.Agent
   :members: forward, start, listen, accept, connect, output, write, end_input, close, release
```
