# SSH clients over rmote

`rmote sshmux` exposes a local OpenSSH control socket. Each ordinary `ssh`
client gets a separate {doc}`Vty session <api/tools/vty>`, and all sessions
share one rmote connection to the target.

Standard `sftp` clients can use the same control socket. SFTP requires only
Python 3.11+ and its standard library on the target POSIX host. No remote
installation of `rmote` or `sftp-server` is needed.

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

## Message of the day

Use `--motd` to show the remote host's message of the day before each shell
session with a PTY:

```bash
rmote sshmux --motd --socket /tmp/my-rmote.sock -- ssh user@server
```

The remote terminal launcher reads `/run/motd.dynamic`, then `/etc/motd`,
before starting the login shell. It skips missing, unreadable, and non-regular
files. If both paths refer to the same file, it displays that file once.
`~/.hushlogin` on the remote host suppresses the message.

MOTD is off by default. Commands, including commands with `ssh -t`, SFTP,
and shells without a PTY do not receive it. The option only displays existing
files: it does not call PAM or run scripts to regenerate the message.
The host must update dynamic MOTD separately. If the mux server is already
running, start a new server or use another socket to apply `--motd`.

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

## One rule for a family of hosts

`ControlPath` and `Match exec` both expand SSH's tokens: `%%`, `%C`, `%d`,
`%h`, `%i`, `%j`, `%k`, `%L`, `%l`, `%n`, `%p`, `%r` and `%u`. The `exec`
command runs under your shell, so a shell expansion can change a token after
SSH substitutes it. One rule therefore serves a whole family of aliases, and
every alias gets a server of its own:

```text
Host *.docker
    ControlPath %d/.ssh/rmote-%C.sock
    ControlMaster no
    ProxyCommand false
    ForwardAgent no
    ForwardX11 no

Match originalhost *.docker exec "n=%n; rmote sshmux --daemon --idle-timeout 300 --socket %d/.ssh/rmote-%C.sock -- docker exec -i ${n%%.docker}"
Match all
```

`ssh my-container.docker` reaches the container `my-container`: SSH replaces
`%n` with the alias, and the shell removes the `.docker` suffix. Another alias
starts another server through the same rule.

```bash
ssh my-container.docker uname -s
ssh other-container.docker 'cat /etc/hostname'
```

Write `%%` for the percent sign that the shell needs. SSH reads `%` as the
start of a token and stops with `unknown key %.` for a single one. It gives
the shell `${n%.docker}` after it replaces `%%` with `%`.

Build the socket path from `%C`, a hash of `%l%h%p%r%j`. It is short, it
differs for every host, and SSH gives `ControlPath` and the `exec` command the
same value. A directory and a host name together pass the 104-byte limit of a
UNIX socket path, and both rmote and SSH then report the length and refuse.

Name the home directory with `%d`, not with `~`. SSH expands `~` from the
account's passwd entry, and the shell of the `exec` command expands it from
`$HOME`. Where those differ, the server listens on one socket and the client
looks for another.

`%n` is the alias from the command line, and it reaches the shell of the
`exec` command without quoting. An alias that contains shell characters runs
them, so use this form for aliases that you write yourself.

## Transport and destination

The connection to the target uses rmote's common {doc}`transports`. SSH is the
client interface to the mux socket; the underlying transport can reach Python
through SSH, Docker/Kubernetes exec, a local process or a prepared byte stream.

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

`rmote sshmux` supports the OpenSSH mux v4 passenger protocol:
shell and exec sessions, health checks and master termination. It requires
POSIX on both sides and an OpenSSH client locally; the remote Python needs no
third-party packages.

## Agent forwarding

To forward an agent, start the mux server with `SSH_AUTH_SOCK`
pointing to your local agent, then request forwarding for a shell or command:

```console
ssh -A container ssh-add -L
```

To select a known local agent socket explicitly, use `--agent-socket` when
starting the mux server. It overrides `SSH_AUTH_SOCK`:

```bash
rmote sshmux --socket /tmp/container.sock --agent-socket /path/to/agent.sock -- docker exec -i my-container
ssh -A -S /tmp/container.sock -o ProxyCommand=false container ssh-add -L
```

The option also works with `--daemon`. Changing it requires restarting an
existing mux server. Clients must still request forwarding with `-A` or
`ForwardAgent yes`.

The forwarded agent belongs to the mux server process. The mux request does
not carry the connecting client's agent path. If you change `SSH_AUTH_SOCK`,
restart the mux server to select the new agent. Each forwarded session has a
private remote Unix socket and up to 32 agent connections. Closing the session
closes those connections and removes its socket and temporary directory.
Forcibly terminating the remote interpreter can leave the temporary directory
behind, though its sockets can no longer forward requests.
An unavailable local agent produces a warning; the shell still opens.
Without `-A` or `ForwardAgent yes`, the shell receives no agent socket,
including one inherited by the remote Python process.

This is a byte bridge to the agent. It does not create an OpenSSH
`session-bind@openssh.com` binding for the rmote hop, which has no SSH host key
or key-exchange signature. Do not rely on destination constraints to validate
this forwarding hop, or on agent restrictions that require identifying a
connection as forwarded. See OpenSSH's
[agent protocol extensions](https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.agent).

X11 forwarding is not implemented. A request produces a warning and the shell
opens without X11 forwarding. Use `ssh -x` to disable the request.

The public `Agent` Tool also supports reverse forwarding and forwarding between
two remote endpoints. Select the listener and agent independently:

```python
from rmote.tools import Agent

# Local agent available through a remote socket.
async with Agent.forward(listener=remote) as remote_socket:
    ...

# Remote agent available through a local socket.
async with Agent.forward(agent=remote) as local_socket:
    ...

# Use a known agent socket on the remote endpoint.
async with Agent.forward(agent=remote, path="/run/user/1000/ssh-agent.sock") as local_socket:
    ...

# Agent on host B available through a socket on host A.
async with Agent.forward(listener=host_a, agent=host_b) as socket_on_a:
    ...
```

Each endpoint is an open `Protocol`; an omitted endpoint means the current
Python process. `path=` selects the agent socket on the agent endpoint. Without
it, the Tool reads that endpoint's `SSH_AUTH_SOCK`. Set the consuming process's
`SSH_AUTH_SOCK` to the yielded listener path. Both endpoint sessions and their
connections are released when the context exits. The coordinator relays bytes
through the selected Protocol connections. The same session-binding limitation
described above applies in every direction.
See the {doc}`Agent API <api/tools/agent>` for runnable examples and the
operations used to manage sessions and connections.

## SFTP without a remote SFTP server

The `sftp` subsystem supports SFTP v3. The target POSIX host
needs only Python 3.11+ and its standard library: no `sftp-server`, installed
`rmote` package, or additional Python packages. rmote transfers the required
Python code automatically and performs file operations through it.

SFTP uses the existing rmote transport. With Docker or Kubernetes exec, the
target does not need OpenSSH or `sshd`. If you choose SSH as the transport,
`sshd` is still required for that connection, but its SFTP subsystem is not used.
The OpenSSH `sftp` client runs locally.

With the SSH configuration above, connect with:

```console
sftp container
```

Without an SSH config entry, specify the control socket explicitly:

```console
sftp -o ControlPath=~/.ssh/container.sock -o ProxyCommand=false container
```

The backend supports regular file reads and writes, resume, directory listing,
rename, symbolic links, and file attributes. It also supports the OpenSSH
`posix-rename` and `fsync` extensions. File changes are applied directly; uploads
do not use FileSync's atomic replacement or block comparison. Paths use the
remote Python process's working directory and permissions.

Each SFTP session owns up to 128 file and directory handles. Requests run in
order, one at a time per session, with reads and writes limited to 256 KiB.
Closing the session releases its handles, including when the mux client
disconnects. Sequential requests limit transfer throughput on connections
with high latency. Special files, extended attributes, and
unadvertised SFTP extensions are not supported.

For direct Python file access, see the {doc}`Files API <api/tools/files>`.
It documents session ownership, descriptor cleanup, and `OpenFlags`.

## Other requests

Other subsystems, port forwarding, mux proxy mode and `ssh -O stop` are not
implemented and receive a failure response.
The adapter does not implement an SSH network server or SSH authentication.
