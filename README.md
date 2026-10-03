# rmote

![rmote](https://raw.githubusercontent.com/mosquito/rmote/master/docs/_static/logo.svg)

[![PyPI Version](https://img.shields.io/pypi/v/rmote.svg)](https://pypi.org/project/rmote/)
[![Python Versions](https://img.shields.io/pypi/pyversions/rmote.svg)](https://pypi.org/project/rmote/)
[![Tests](https://github.com/mosquito/rmote/actions/workflows/tests.yml/badge.svg)](https://github.com/mosquito/rmote/actions/workflows/tests.yml)
[![Docs](https://github.com/mosquito/rmote/actions/workflows/docs.yml/badge.svg)](https://docs.rmote.org)

rmote runs Python functions on another machine over SSH. Use its built-in tools
to manage files, packages, and services, or write a tool for your application.
Calls return Python values and propagate remote exceptions to the caller.

Install rmote on your local machine. The target needs Python 3.11 or newer,
but no rmote installation or agent. Tool code is sent when it is first used
on a connection. Both machines must run a supported POSIX system; Windows
remote hosts are not supported.

## Install

```bash
python -m pip install rmote
```

Python 3.11 or newer is required locally too. Package and service tools also
need the target's system commands and sufficient permissions. rmote does not
install tool dependencies or elevate privileges automatically.

## Read a Remote File

Replace `user@server` with a host you can reach using your SSH configuration.
Save this as `read_hosts.py` and run it with `python read_hosts.py`:

<!-- name: test_readme; fixtures: docs_ssh; mark: timeout(20) -->
```python
from rmote.sync import Connection
from rmote.tools import FileSystem

with Connection.from_ssh("user@server") as remote:
    hosts = remote(FileSystem.read_str, "/etc/hosts")
    assert "localhost" in hosts
    print(hosts, end="")
```

The file is read on the target, with the SSH user's permissions. The `with`
block closes the connection. `Connection.from_local()` runs the same tools
in a local subprocess; async applications use `rmote.protocol.Protocol`.

The [quickstart](https://docs.rmote.org/quickstart.html) walks
through deploying Redis to a Docker container: install its package, render a
systemd unit, start the service, and verify it. Switching to SSH changes only
the connector.

## Guides

- [Writing tools](https://docs.rmote.org/writing-tools.html): define a remote operation and return Python data.
- [Multiple hosts](https://docs.rmote.org/multi-host.html): run operations concurrently and handle individual failures.
- [Templates](https://docs.rmote.org/templating.html): render configuration files.
- [Built-in tools](https://docs.rmote.org/api/tools/index.html): files, commands, packages, services, users, and logging.
- [Connection reference](https://docs.rmote.org/api/sync.html): SSH options, deadlines, and cleanup.
- [Execution model](https://docs.rmote.org/concepts.html): code transfer, state, concurrency, and cancellation.

Use rmote only with trusted hosts and tool code. It transfers executable Python
and uses pickle for results. A timeout stops local waiting; it does not cancel
an operation already running on the target.

## Development

```bash
uv sync --group dev --group docs
make test
make docs
make docs-test
```

Some integration tests require Docker; SSH tests need local OpenSSH binaries.
Use `uv run pytest --no-docker` to exclude Docker tests. Named documentation
examples run through `markdown-pytest` alongside the Python tests.

rmote is beta software. See the [release notes](https://docs.rmote.org/release-notes.html)
for changes and compatibility. Licensed under [Apache 2.0](LICENSE).

Documentation pages use MyST Markdown (`.md`) and are built with Sphinx.
API pages include docstrings through `autodoc` in `{eval-rst}` blocks.
Runnable Markdown examples are checked by `markdown-pytest` via `make docs-test`
and the regular test suite. Both also run API doctests directly from the
docstrings in `rmote` using pytest's `--doctest-modules`. To run only API
doctests, use `uv run pytest rmote`.
