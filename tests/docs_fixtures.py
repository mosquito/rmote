"""SSH transport for executable documentation with placeholder hostnames."""

import asyncio
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from rmote.protocol import Protocol
from rmote.sync import Connection
from tests.test_sync_connection import local_sshd as local_sshd


@pytest.fixture
def client_resources(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[asyncio.subprocess.Process]]:
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
