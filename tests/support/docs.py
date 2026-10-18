"""SSH transport for executable documentation with placeholder hostnames."""

import asyncio
import sys
import threading
from collections.abc import Iterator
from functools import partial
from typing import Any

import pytest

from rmote.protocol import Protocol, TLSConfig
from rmote.sync import Connection
from tests.support.ssh import local_sshd as local_sshd
from tests.support.tls import tls_for_mode


def subprocess_example(source: str, path: str, enabled: bool, caller: Any) -> None:
    # The inline-tool example needs __main__ and a real source file. Inject TLS
    # before its source runs without changing the published example.
    prefix = (
        "from rmote.protocol import Protocol, TLSConfig\n"
        "_factory = Protocol.from_subprocess\n"
        "async def _with_tls(cls, process, *, tls=None):\n"
        f"    return await _factory(process, tls=tls if tls is not None else {tls_for_mode(enabled)!r})\n"
        "Protocol.from_subprocess = classmethod(_with_tls)\n"
    )
    caller(prefix + source, path)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Markdown collectors create Functions without pytest's parameter expansion.
    expanded: list[pytest.Item] = []
    for item in items:
        if not isinstance(item, pytest.Function) or item.path.suffix != ".md":
            expanded.append(item)
            continue
        call = item.obj
        subprocess_call = isinstance(call, partial) and any(
            name in call.args[0] for name in ("Connection.from_", "Protocol.from_")
        )
        if "docs_tls" not in item.fixturenames and not subprocess_call:
            expanded.append(item)
            continue
        assert item.parent is not None
        for enabled, mode in ((False, "plaintext"), (True, "tls")):
            variant_call = (
                partial(subprocess_example, call.args[0], call.args[1], enabled, call.func)
                if subprocess_call
                else call
            )
            variant = pytest.Function.from_parent(item.parent, name=f"{item.name}[{mode}]", callobj=variant_call)
            variant.own_markers.extend(item.own_markers)
            variant.keywords.update(item.keywords)
            variant.user_properties.append(("transport_tls", enabled))
            expanded.append(variant)
    items[:] = expanded


@pytest.fixture
def docs_tls(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Run unchanged documentation examples through each real transport mode."""
    transport_tls = tls_for_mode(dict(request.node.user_properties)["transport_tls"])
    factory = Protocol.from_subprocess

    async def from_subprocess(
        cls: type[Protocol],
        process: asyncio.subprocess.Process,
        *,
        tls: TLSConfig | None = None,
        decode_threshold: int = Protocol.DECODE_INLINE,
    ) -> Protocol:
        return await factory(process, tls=tls if tls is not None else transport_tls, decode_threshold=decode_threshold)

    monkeypatch.setattr(Protocol, "from_subprocess", classmethod(from_subprocess))


@pytest.fixture
def client_resources(monkeypatch: pytest.MonkeyPatch, docs_tls: None) -> Iterator[list[asyncio.subprocess.Process]]:
    """Check process and thread cleanup after an executable client example."""
    threads = set(threading.enumerate())
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def tracked_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked_spawn)
    yield processes
    assert all(process.returncode is not None for process in processes)
    assert set(threading.enumerate()) == threads


@pytest.fixture
def docs_ssh(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    client_resources: list[asyncio.subprocess.Process],
) -> Iterator[None]:
    """Redirect documentation hostnames to an isolated real SSH server."""
    port, key, user = request.getfixturevalue("local_sshd")
    options = [
        "-o",
        "BatchMode=yes",
        "-S",
        "none",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
    ]
    sync_factory = Connection.from_ssh
    async_factory = Protocol.from_ssh

    def sync_ssh(cls: type[Connection], /, host: str, **kwargs: Any) -> Connection:
        for name in ("user", "port", "identity", "python", "ssh_options"):
            kwargs.pop(name, None)
        return sync_factory(
            "127.0.0.1",
            user=user,
            port=port,
            identity=key,
            python=sys.executable,
            ssh_options=options + (["-o", "ProxyCommand=false"] if host == "unreachable.example" else []),
            **kwargs,
        )

    async def async_ssh(cls: type[Protocol], /, host: str, **kwargs: Any) -> Protocol:
        for name in ("user", "port", "identity", "python", "ssh_options"):
            kwargs.pop(name, None)
        return await async_factory(
            "127.0.0.1",
            user=user,
            port=port,
            identity=key,
            python=sys.executable,
            ssh_options=options + (["-o", "ProxyCommand=false"] if host == "unreachable.example" else []),
            **kwargs,
        )

    monkeypatch.setattr(Connection, "from_ssh", classmethod(sync_ssh))
    monkeypatch.setattr(Protocol, "from_ssh", classmethod(async_ssh))
    yield
    assert client_resources, "The documentation must execute a real SSH subprocess"
