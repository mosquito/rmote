# Interactive shell

`rmote-shell` opens an interactive shell on a host. The transport command starts
a Python interpreter at the far end, rmote bootstraps its protocol into that
interpreter, and the {doc}`Vty tool <api/tools/vty>` opens a session there.

```bash
rmote-shell ssh server
```

The remote side needs only a Python interpreter and the standard library. No
agent and no package installation are required.

## The transport is a command

Everything after the client options is the transport command. rmote appends the
interpreter and its flags, so `rmote-shell ssh server` runs
`ssh server python3 -qui`.

Any command works when it passes stdin and stdout through without changing the
bytes:

```bash
rmote-shell ssh -p 2222 -i ~/.ssh/id_ed25519 user@server
rmote-shell docker exec -i my-container
rmote-shell kubectl exec -i pod/my-pod --
rmote-shell sudo -u postgres
rmote-shell                                  # a session on the local host
```

Use `--python` when the interpreter has another name or path:

```bash
rmote-shell --python /usr/local/bin/python3 ssh server
```

## Terminal or pipes

The client looks at its own descriptors. With a terminal on both stdin and
stdout the session gets a pseudo terminal, which gives job control, a window
size and full screen programs. With a redirected input or output the session
gets pipes instead:

```bash
rmote-shell ssh server                       # a terminal
rmote-shell --command cat ssh server < file  # pipes
```

Pipes keep the bytes exactly. No carriage return is added, binary data passes
through, and the end of the local input closes the input of the remote child, so
a program such as `cat` finishes. A terminal has no half close, so there the end
of input is reported with the VEOF character, normally Ctrl-D.

`--no-pty` asks for pipes even from a terminal, for the times when exact
bytes matter more than line editing:

```bash
rmote-shell --no-pty --command cat ssh server
```

A host that cannot give a terminal reports it and names this option, so the
session is still reachable there.

## Choosing what runs

Without `--command` the session starts the login shell of the remote user.
Repeat the option for each argument. An argument that starts with a dash needs
the `=` form, because the option parser would otherwise read it as an option:

```bash
rmote-shell --command /bin/bash --command=-l ssh server
rmote-shell --command htop ssh server
```

## Closing the session

In raw mode every key goes to the remote session, so Ctrl-C reaches the remote
program instead of the client. The escape sequence works directly after a line
end, the same rule as in ssh:

| Sequence | Effect |
|---|---|
| `~.` | Close the session. The remote child ends with it. |
| `~~` | Send one literal `~`. |

The remote child ends with the session. There is no saved session to come back
to. `--escape` changes the escape character, and an empty value disables the
sequence:

```bash
rmote-shell --escape ^ ssh server
rmote-shell --escape "" ssh server
```

## Window size and TERM

The client reports the local window size when the session opens, and again on
every window change. `TERM` is copied from the local environment, and `--term`
overrides it.

## Troubleshooting

A start that fails names its cause: the last lines the transport wrote to its
stderr go into the error, so a refusal of `ssh` or an interpreter that cannot
run appears instead of a hang. `--transport-stderr` shows that stream live
while the session runs, and `--debug` adds protocol logs:

```bash
rmote-shell --transport-stderr --debug ssh server
```

The exit status of `rmote-shell` is the exit status of the remote child. A
command that does not exist is reported through the session and gives status
127, as a shell does.

## Sessions from Python

The tool works without the command line client. Open a session, read its output
through a streaming call, and close it to get the exit status.

<!-- name: test_shell_doc; case: pipe_session; mark: timeout(30) -->
```python
import asyncio
import sys

from rmote.protocol import Protocol
from rmote.tools import Vty


async def run_through_a_pipe() -> tuple[bytes, int]:
    # An empty transport starts the interpreter on the local host.
    protocol = await Protocol.from_command(python=sys.executable)
    async with protocol:
        key = await protocol(Vty.open, ["cat"], want_pty=False)
        await protocol(Vty.write, key, b"hello from the session\n")
        await protocol(Vty.end_input, key)

        output = bytearray()
        async for chunk in protocol(Vty.output, key):
            output += chunk

        return bytes(output), await protocol(Vty.close, key)


output, status = asyncio.run(run_through_a_pipe())
assert output == b"hello from the session\n"
assert status == 0
```

A terminal session needs the source of the controlling terminal helper, because
a transferred module has no file of its own:

```python
from rmote.tools.vty import Vty, launcher_source

key = await protocol(Vty.open, ["/bin/sh"], rows=24, cols=80, launcher=launcher_source())
```

Several sessions can run at once on one connection, and ordinary calls keep
working while a session streams output. See
[streaming calls](api/protocol.md) for the mechanism.

## Limits

Both sides need a POSIX host. The remote session uses a pseudo terminal, and
the client puts its own terminal in raw mode.

The client does not forward ports, does not forward an authentication agent and
does not copy files. Use `ssh` and the rmote file tools for that.
