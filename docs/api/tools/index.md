# Built-in Tools

Import built-in tools from `rmote.tools`. Pass a tool method and its arguments
to `Connection` or `Protocol` to execute it on the connected host.
Module tools such as `facts` expose functions called in the same way.
Tools use that connection's user permissions and the programs installed there.

`FileSync.upload/download` and `Rsync.upload/download` coordinate local and remote files:
call them directly with an open async `Protocol`. See {doc}`file_sync` and {doc}`rsync`.

`Agent.forward` is an async context manager called directly. It connects an
agent and a listener on independently selected endpoints. See {doc}`agent`.

`facts.fetch(remote, ...)` collects host state through an open async connection;
`await remote(facts.gather, ...)` collects it in one remote call. See {doc}`facts`.

Each page documents the tool's operations first, followed by its related types.
See {doc}`../../quickstart` for a complete deployment example and
{doc}`../../writing-tools` for defining your own tools.

## What a tool needs from its target

A tool that reads a kernel interface of Linux, or runs the program of one
distribution, refuses on another target with `NotImplementedError`. The message
names the operation, what it needs and what the host is, because the failure
travels through RPC and the caller sees nothing else:

```text
Sysctl.get needs the /proc/sys interface of Linux, and this host runs Darwin
Service needs the systemctl program of systemd, and this host has none
AptRepository.absent needs /etc/apt of Debian and Ubuntu, and this host has none
```

| Tool | Needs |
| --- | --- |
| `FileSystem`, `Files`, `Exec`, `Template`, `FileSync`, `Rsync`, `Logger`, `Quit` | any POSIX target |
| `Agent` | POSIX Unix sockets; an existing SSH agent on the selected agent endpoint |
| `Hostname` | `get` and `hosts_entry` any target; `set` the `/proc/sys` interface of Linux |
| `Sysctl` | the `/proc/sys` interface of Linux; `absent` only edits the persistent file |
| `Service` | the `systemctl` program of systemd |
| `User` | the shadow utilities for users and groups; `sudoer` and `authorized_key` any target |
| `Apt`, `AptRepository` | `apt-get` and `/etc/apt` of Debian and Ubuntu |
| `Pacman`, `PacmanRepository` | `pacman` and `/etc/pacman.conf` of Arch Linux |
| `Vty` | a pseudo terminal of POSIX for a terminal session; the pipe mode needs none |

The checks live in {doc}`../requires`. A tool of your own can use them for
the same message.

```{toctree}
:maxdepth: 1
:caption: Files and templates

filesystem
files
file_sync
rsync
template
```

```{toctree}
:maxdepth: 1
:caption: Commands and services

exec
service
vty
agent
```

```{toctree}
:maxdepth: 1
:caption: System configuration

user
hostname
sysctl
facts
```

```{toctree}
:maxdepth: 1
:caption: Packages and repositories

apt
apt_repository
pacman
pacman_repository
```

```{toctree}
:maxdepth: 1
:caption: Logging and shutdown

logger
quit
```
