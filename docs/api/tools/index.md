# Built-in Tools

Import built-in tools from `rmote.tools`. Pass a tool method and its arguments
to `Connection` or `Protocol` to execute it on the connected host.
Tools use that connection's user permissions and the programs installed there.

`FileSync.upload/download` and `Rsync.upload/download` coordinate local and remote files:
call them directly with an open async `Protocol`. See {doc}`file_sync` and {doc}`rsync`.

Each page documents the tool's operations first, followed by its related types.
See {doc}`../../quickstart` for a complete deployment example and
{doc}`../../writing-tools` for defining your own tools.

```{toctree}
:maxdepth: 1
:caption: Files and templates

filesystem
file_sync
rsync
template
```

```{toctree}
:maxdepth: 1
:caption: Commands and services

exec
service
```

```{toctree}
:maxdepth: 1
:caption: System configuration

user
hostname
sysctl
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
