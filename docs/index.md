# rmote

rmote runs Python functions through SSH, `docker exec`, `kubectl exec`, a
local Python process, or another bidirectional stream connected to Python.
See {doc}`transports` for the common model, including prepared `nc` relays.
Built-in tools manage files, commands, packages, services, and users. You can
also define your own tools and return Python values from them.

Install rmote locally with Python 3.11 or newer:

```bash
python -m pip install rmote
```

The target needs Python 3.11 or newer but no rmote installation. rmote sends
tool code over the connection when it is first used. Package and service tools
require their system commands and sufficient permissions on the target.

To operate without an agent, rmote runs standard `python3 -qui` on the target:

- `-q` (**quiet**): suppresses Python's startup banner and copyright notices.
- `-u` (**unbuffered**): forces unbuffered stdout/stderr streams for instant packet delivery.
- `-i` (**interactive**): reads and executes statements from stdin even without a TTY.

A free-threaded interpreter works on both sides. rmote has no compiled
extension, and it guards the state it shares between threads with its own
locks. The tests cover 3.13t and 3.14t on Linux and macOS.

## Start with a Deployment

Follow the {doc}`quickstart` to deploy a Redis cache in a disposable Docker
container. You will install a package, render a systemd unit, start the service,
and check that it works. The deployment code also works over SSH by replacing
the connector.

## Choose a Guide

| Task | Guide |
|---|---|
| Connect through SSH, containers, local Python or another byte stream | {doc}`transports` |
| Write a remote operation and return Python data | {doc}`writing-tools` |
| Explore a host from a Python console | {doc}`repl` |
| Collect and cache host state | {doc}`api/tools/facts` |
| Apply an operation to several hosts | {doc}`multi-host` |
| Synchronize file contents in either direction | {doc}`api/tools/file_sync` |
| Synchronize files or trees from the command line | {doc}`rsync` |
| Render a configuration file | {doc}`templating` |
| Write and transfer a custom template filter | {doc}`template-filters` |
| Find a built-in operation | {doc}`api/tools/index` |
| Configure synchronous connections and deadlines | {doc}`api/sync` |
| Use connections from an async application | {doc}`api/protocol` |
| Understand code transfer, state, and cancellation | {doc}`concepts` |

Use `rmote.sync.Connection` for synchronous scripts and `rmote.protocol.Protocol`
for async applications. Both clients call the same tools. Use rmote only with
trusted hosts and tool code: the target executes transferred Python, and the
client unpickles returned data.

```{toctree}
:maxdepth: 2
:caption: User Guide

quickstart
transports
writing-tools
repl
rsync
shell
sshmux
multi-host
templating
template-filters
concepts
```

```{toctree}
:maxdepth: 1
:caption: Releases

release-notes
```

```{toctree}
:maxdepth: 2
:caption: API Reference

api/index
```
