# rmote

rmote runs Python functions on another machine over SSH. Built-in tools manage
files, commands, packages, services, and users. You can also define your own
tools and return Python values from them.

Install rmote locally with Python 3.11 or newer:

```bash
python -m pip install rmote
```

The target needs Python 3.11 or newer but no rmote installation. rmote sends
tool code over the connection when it is first used. Package and service tools
require their system commands and sufficient permissions on the target.
Windows remote hosts are not supported.

## Start with a Deployment

Follow the {doc}`quickstart` to deploy a Redis cache in a disposable Docker
container. You will install a package, render a systemd unit, start the service,
and check that it works. The deployment code also works over SSH by replacing
the connector.

## Choose a Guide

| Task | Guide |
|---|---|
| Write a remote operation and return Python data | {doc}`writing-tools` |
| Apply an operation to several hosts | {doc}`multi-host` |
| Render a configuration file | {doc}`templating` |
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
writing-tools
multi-host
templating
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
