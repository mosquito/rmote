# Transports

rmote runs tools over a bidirectional byte stream connected to a Python
interpreter. SSH, `docker exec`, `kubectl exec` and a local Python process
all provide that connection. A relay such as `nc` also works when its other
end is connected to a process that can execute Python.

The transport supplies the connection; rmote sends its bootstrap and the
required tool code through it. The protocol needs Python 3.11+ and its standard
library on the target, with no rmote installation or separate agent.
Tools keep the same API whichever transport you choose. Their own requirements
still apply: for example, Rsync needs no external transfer software, while
package and service tools need the corresponding system commands.

## Commands that start Python

`Connection.from_command` and `Protocol.from_command` take a command prefix
and append the Python executable and `-qui`. For example,
`from_command("ssh", "-T", "server")` starts `ssh -T server python3 -qui`.
rmote writes the bootstrap to that interpreter and loads tools when needed.

| Destination | Command prefix | Process started by rmote |
|---|---|---|
| SSH host | `ssh -T server` | `ssh -T server python3 -qui` |
| Docker container | `docker exec -i my-container` | `docker exec -i my-container python3 -qui` |
| Kubernetes pod | `kubectl exec -i pod/my-pod --` | `kubectl exec -i pod/my-pod -- python3 -qui` |
| Local machine | No command prefix | A local Python process with `-qui` |

The appended `-qui` flags are [standard Python interpreter options](https://docs.python.org/3/using/cmdline.html):

- `-q` (**quiet**): suppresses Python's startup banner and copyright notices.
- `-u` (**unbuffered**): forces stdout and stderr to be unbuffered so protocol packets are sent immediately.
- `-i` (**interactive**): reads and executes statements from stdin even without a pseudo-terminal (TTY).

Select the interpreter with the factories' `python=` argument or the CLI's
`-p` / `--python`. The factories use `python3`; pass `python=sys.executable`
to use the interpreter running your application. `Connection.from_local()`
is a shortcut for a local subprocess. The `repl` and `rsync` commands use
their own interpreter when the transport is omitted; `shell` and `sshmux`
use `python3` unless overridden.

The transport must keep stdin open and pass bytes in both directions without
terminal processing. Use SSH `-T` and Docker/Kubernetes `exec -i`, without
`-t`. Keep diagnostic output on stderr. The interactive terminal of
`rmote shell` or an sshmux session is created separately by the Vty tool;
the transport itself carries binary protocol traffic.

Docker and Kubernetes supply access to the container directly: an SSH server
inside it is unnecessary. The same rule applies to a local Python subprocess.

## Selecting a transport in the CLI

{doc}`repl`, {doc}`shell` and {doc}`sshmux` accept the command and its arguments
after their own options. Use `--` to mark that boundary explicitly:

```bash
rmote repl -- ssh -T server
rmote shell -- docker exec -i my-container
rmote repl -- kubectl exec -i pod/my-pod --
rmote sshmux -S ./container.sock -- docker exec -i my-container
rmote repl                                      # local Python subprocess
```

{doc}`rsync` takes the command as one quoted string with `-r` / `--exec`:

```bash
rmote rsync -r 'ssh -T server' ./project remote:/srv/project
rmote rsync -r 'docker exec -i my-container' ./project remote:/app
rmote rsync -r 'kubectl exec -i pod/my-pod --' ./project remote:/app
rmote rsync ./project remote:./project-copy       # local Python subprocess
```

rmote appends the interpreter and `-qui` in each case. The `remote:` prefix in
rsync identifies the subprocess side, even when that process runs locally.

Command prefixes are executed as argument lists. Rsync splits its quoted
command with shell-style quoting; pipes, redirects and variable expansion
require an explicitly invoked shell.

## Relays and already prepared streams

`nc` can carry the same protocol when the receiving side starts
`python3 -qui` and connects the accepted stream to its stdin and stdout.
The relay carries bytes; the receiving setup is responsible for launching
Python. After that, rmote supplies the bootstrap and tool code as usual.

For such a relay, create the subprocess with stdin/stdout pipes and pass it
to {meth}`~rmote.protocol.BaseProtocol.from_subprocess`. This factory sends
the bootstrap through the existing streams without appending command-line
arguments. The caller owns that subprocess and must stop and reap it;
`from_command` and `from_ssh` own their subprocesses themselves.

The CLI uses `from_command`, so a relay adapter must account for the appended
Python arguments. For example, if `host:9000` already connects each accepted
stream to `python3 -qui`, this shell wrapper runs the relay and leaves the
appended arguments unused:

```bash
rmote repl -- sh -c 'exec nc host 9000' rmote-nc
```

Here `rmote-nc` supplies the shell's `$0`; the appended interpreter and flags
become unused positional parameters. A bare `nc host 9000` does not implement
the command-launching interface expected by `from_command`.

Authentication and encryption come from the transport. SSH supplies both;
a plain `nc` connection does not add them. The protocol executes transferred
Python and unpickles responses, so both endpoints must be trusted.

See {doc}`api/sync` and {doc}`api/protocol` for connection factories and resource
ownership, and {doc}`concepts` for bootstrap and code transfer details.
