# rmote

![rmote](https://raw.githubusercontent.com/mosquito/rmote/master/docs/_static/logo.svg)

[![PyPI Version](https://img.shields.io/pypi/v/rmote.svg)](https://pypi.org/project/rmote/)
[![Python Versions](https://img.shields.io/pypi/pyversions/rmote.svg)](https://pypi.org/project/rmote/)
[![Tests](https://github.com/mosquito/rmote/actions/workflows/tests.yml/badge.svg)](https://github.com/mosquito/rmote/actions/workflows/tests.yml)
[![Docs](https://github.com/mosquito/rmote/actions/workflows/docs.yml/badge.svg)](https://docs.rmote.org)

rmote runs Python functions through SSH, `docker exec`, `kubectl exec`, or a
local Python subprocess. Any bidirectional stream that reaches a Python
interpreter can carry its protocol, including an appropriately connected `nc`
relay. See [Transports](https://docs.rmote.org/transports.html) for setup and examples.
Use its built-in tools to manage files, packages, and services, or write a tool for your application.
Calls return Python values and propagate remote exceptions to the caller.

Install rmote on your local machine. The target needs Python 3.11 or newer,
but no rmote installation or agent. Tool code is sent when it is first used
on a connection.

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

## Interactive Shell

`rmote shell` opens an interactive shell on a host. The transport is any command
that passes stdin and stdout through unchanged, so the same client reaches hosts
over SSH, containers and pods.

```bash
rmote shell ssh server
rmote shell docker exec -i my-container
rmote shell kubectl exec -i pod/my-pod --
```

`python -m rmote` accepts the same commands and options as `rmote`:
for example, `python -m rmote shell ssh server`. Run `rmote --help` to list
commands, or `rmote shell --help` for shell options.

The remote side opens a real pseudo terminal, so job control, full screen
programs and window resizing all work. A redirected input or output uses pipes
instead and keeps the bytes exactly. Press `~.` after a line end to close the
session.

## Python REPL

Open a Python console with a ready connection and remote host facts:

```bash
rmote repl -- ssh -T server
rmote repl -- docker exec -i my-container
rmote repl --async -- ssh -T server
```

Use `host["system"]` to inspect the host, or
`remote(facts.gather, sections=["cpu", "memory"])` to collect more facts.
With `--async`, write `await remote(...)`. Built-in tools are imported and
you can define your own `Tool` in the console. See the
[REPL guide](https://docs.rmote.org/repl.html) for examples and script mode.

## Guides

- [Transports](https://docs.rmote.org/transports.html): SSH, Docker, Kubernetes, local Python and prepared byte streams.
- [Synchronization CLI](https://docs.rmote.org/rsync.html): `rmote rsync -r 'ssh -T host' ./source remote:/destination`, or the reverse direction.
- [Writing tools](https://docs.rmote.org/writing-tools.html): define a remote operation and return Python data.
- [Interactive shell](https://docs.rmote.org/shell.html): run a shell on a host through any transport command.
- [Multiple hosts](https://docs.rmote.org/multi-host.html): run operations concurrently and handle individual failures.
- [Templates](https://docs.rmote.org/templating.html): render configuration files.
- [Built-in tools](https://docs.rmote.org/api/tools/index.html): files, commands, packages, services, users, host facts, and logging.
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

Tests are grouped by module: `tests/protocol`, `tests/sync`, `tests/tools`,
`tests/facts` (including `tests/facts/collectors`), `tests/cache`,
`tests/immutable`, `tests/process`, and `tests/templates`. Fixtures are scoped
through local `conftest.py` files; shared implementations live in `tests/support`.

Run `uv run pytest tests/cache` to check the general cache, its JSON and SQLite
backends, and serialization. `uv run pytest tests/facts` checks collection,
result validation, and integration with the cache. For a local run of both
areas, use `uv run pytest tests/cache tests/facts --no-docker`.

Collector integration tests use disposable Debian/systemd and Arch Docker
containers, without an external SSH host. The systemd fixture uses Debian
Forky for JSON-capable `resolvectl status`; package tests also cover Debian
Trixie and Arch. The networkd/iproute2 tests also run on Ubuntu Noble,
including managed DNS/NTP settings and cache refresh after networkd stops.
Run `uv run pytest tests/facts -m docker` for those integration checks, or
`uv run pytest tests/facts --no-docker` for local collection and parser tests.
The systemd container needs privileged Docker with private network/cgroup namespaces;
timesyncd stays masked so tests cannot change the host clock.

rmote is beta software. See the [release notes](https://docs.rmote.org/release-notes.html)
for changes and compatibility. Licensed under [Apache 2.0](LICENSE).

Documentation pages use MyST Markdown (`.md`) and are built with Sphinx.
API pages include docstrings through `autodoc` in `{eval-rst}` blocks.
Runnable Markdown examples are checked by `markdown-pytest` via `make docs-test`
and the regular test suite. Both also run API doctests directly from the
docstrings in `rmote` using pytest's `--doctest-modules`. To run only API
doctests, use `uv run pytest rmote`.
