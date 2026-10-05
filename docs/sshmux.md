# SSH clients over rmote

`rmote sshmux` exposes a local OpenSSH control socket. Each ordinary `ssh`
client gets a separate {doc}`Vty session <api/tools/vty>`, and all sessions
share one rmote connection to the target.

Start the server in one terminal:

```bash
python -m rmote sshmux --socket=/tmp/my-rmote.sock ssh user@server
```

The console script is equivalent:

```bash
rmote sshmux --socket=/tmp/my-rmote.sock ssh user@server
```

`-S` is the short form of `--socket`, matching the SSH client's socket flag.
Use `-v` / `--debug` for protocol logs and `-h` / `--help` for all options.

In other terminals, connect to that socket:

```bash
ssh -S /tmp/my-rmote.sock user@server
ssh -S /tmp/my-rmote.sock user@server 'uname -a'
printf 'hello\n' | ssh -S /tmp/my-rmote.sock user@server cat
```

The server runs in the foreground. Exiting one shell closes only its Vty.
There are no detached sessions to reconnect to.

## Automatic startup from SSH config

`-d` / `--daemon` starts the server in the background and returns after its transport
and control socket are ready. If a server already answers on that socket, it
returns successfully without starting another transport. Concurrent invocations
share a startup lock.

```bash
rmote sshmux --daemon --idle-timeout 300 --socket ~/.ssh/container.sock -- docker exec -i my-container
```

To start it automatically with `ssh container`, put this near the beginning of
`~/.ssh/config`, before general `Host *` defaults:

```text
Host container
    ControlPath ~/.ssh/container.sock
    ControlMaster no
    ProxyCommand false
    ForwardAgent no
    ForwardX11 no

Match originalhost container exec "rmote sshmux --daemon --idle-timeout 300 --socket ~/.ssh/container.sock -- docker exec -i my-container"
Match all
```

Replace `my-container` with the running container's name. If `rmote` is not on
the PATH used by SSH, use its absolute path, such as
`/home/me/project/.venv/bin/rmote`, inside the `exec` command.

```bash
ssh container
ssh container 'uname -a'
printf 'hello\n' | ssh container cat
```

The first connection starts rmote; subsequent connections reuse it. Each SSH
client still gets an independent session. `-i 300` / `--idle-timeout 300` closes the
server and its transport after five minutes without connected clients. A quiet
but open shell keeps the server alive. The default is `0`, which disables this
timeout. The option also works in foreground mode.

`Match exec` runs while SSH reads its configuration, before it connects to
`ControlPath`. A regular `ProxyCommand` expects an SSH server protocol stream,
so it cannot connect directly to rmote's mux protocol. Here `ProxyCommand false`
prevents a failed mux connection from falling back to a network connection.
See OpenSSH's [Match](https://man.openbsd.org/ssh_config#Match) and
[ProxyCommand](https://man.openbsd.org/ssh_config#ProxyCommand) documentation.

For an SSH transport, replace `docker exec -i my-container` with
`ssh -T -o BatchMode=yes user@real-server`. Use a different host alias for that
transport so it does not invoke the same startup rule recursively. A detached
server has no controlling terminal: transport authentication must work without
interactive prompts, for example through an SSH agent.

The socket selects the existing connection. Changing the transport, `--python`
or timeout options does not reconfigure an already running server; stop it
first. Background output goes to `~/.ssh/container.sock.log` (mode 0600).
Startup waits up to 30 seconds and reports failures to stderr. The adjacent
`.lock` file remains after shutdown; leave it in place so launchers use the
same lock. `--daemon` can recover an owned socket left by a crashed server
when it refuses connections; it does not replace regular files, symlinks or
unresponsive listeners.

The startup rule also runs for `ssh -G container`, `ssh -O check container`
and `ssh -O exit container`. To inspect or stop a server without triggering
automatic startup, bypass the configuration:

```bash
ssh -F /dev/null -S ~/.ssh/container.sock -O check container
ssh -F /dev/null -S ~/.ssh/container.sock -O exit container
```

## Transport and destination

Everything after the server options is the transport command. rmote appends
`python3 -qui`; use `-p` / `--python` to choose another interpreter. With no transport,
the interpreter runs locally:

```bash
rmote sshmux --socket=/tmp/container.sock docker exec -i my-container
rmote sshmux --socket=/tmp/local.sock
rmote sshmux --socket=/tmp/server.sock --python /usr/bin/python3 ssh user@server
```

For Docker, use `exec -i`, without `-t`, and omit `python3 -qui` from the
transport command. Docker must pass the binary protocol through pipes; Vty
creates the interactive terminals inside the container afterwards. `exec -it`
fails because the transport's stdin is not a terminal.

The socket selects an already connected destination and user. The host and user
on subsequent `ssh` commands do not change that connection. No sshd is needed
in a container reached through `docker exec`.

OpenSSH normally falls back to a network connection when a control socket is
unavailable or refuses a request. To require this socket, use
`-o ProxyCommand=false`. For example, a host alias can keep that setting together
with the socket path:

```text
Host rmote-container
    ControlPath /tmp/container.sock
    ControlMaster no
    ProxyCommand false
    ForwardAgent no
    ForwardX11 no
```

Then `ssh rmote-container` opens a Vty in the container. Only the user who
started rmote can access the socket (mode 0600). Its parent directory must
already exist, and the socket path must be unused. The server never replaces
an existing file or socket in foreground mode; `--daemon` reuses a running
server as described above.

## Terminal behavior

OpenSSH chooses whether to request a terminal: use `-t` for an interactive
command or `-T` for pipes. Each terminal has its own window size; resize,
Ctrl-C, job control and `TERM` reach the remote session. Pipe sessions preserve
binary data, deliver EOF to stdin, and keep stdout and stderr separate.
Exit status is returned to `ssh`.

Commands run through the remote user's shell with `-c`. An empty command opens
a login shell. Environment entries explicitly sent by the client with
`SendEnv` or `SetEnv` are passed to the child.

The supported escape sequences at the start of a line are `~.` to close this
session and `~~` to send a literal tilde. `ssh -e none` disables escapes.
Other OpenSSH escape commands are not implemented. Remote terminals currently
use their default terminal modes rather than copying local `stty` settings.

## Stopping the server

```bash
ssh -S /tmp/my-rmote.sock -O check user@server
ssh -S /tmp/my-rmote.sock -O exit user@server
```

Ctrl-C, SIGTERM and SIGHUP also stop the server. Shutdown closes active Vty
sessions, the rmote transport and the socket. A client disconnect closes its
session and signals its child process group. Processes that deliberately detach
from the session can survive. An abrupt loss of the remote interpreter cannot
guarantee cleanup of processes on that host.

## Supported OpenSSH requests

This initial implementation supports the OpenSSH mux v4 passenger protocol:
shell and exec sessions, health checks and master termination. It requires
POSIX on both sides and an OpenSSH client locally; the remote Python needs no
third-party packages.

Agent and X11 forwarding are not implemented. If your SSH configuration enables
them, the client receives a warning and the shell opens without forwarding.
Use `ssh -a -x` to disable these requests and their warnings explicitly.

Subsystems (including SFTP), port forwarding, mux proxy mode and `ssh -O stop`
are not implemented and receive a failure response.
The adapter does not implement an SSH network server or SSH authentication.
