"""Real local and SSH transports with a remote interpreter that uses no site-packages."""

import shlex
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from rmote.sync import Connection
from tests.sync_integration_tools import TransportChecks

pytestmark = pytest.mark.timeout(20)


@pytest.fixture(params=["local", "ssh"])
def isolated_connection(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Connection]:
    executable = tmp_path / "isolated-python"
    executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -S "$@"\n')
    executable.chmod(0o755)
    threads = set(threading.enumerate())
    if request.param == "ssh":
        port, key, user = request.getfixturevalue("local_sshd")
        connection = Connection.from_ssh(
            "127.0.0.1",
            user=user,
            port=port,
            identity=key,
            python=str(executable),
            ssh_options=[
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "IdentitiesOnly=yes",
            ],
            stderr=subprocess.PIPE,
            connect_timeout=5.0,
            rpc_timeout=5.0,
        )
    else:
        connection = Connection.from_local(python=str(executable), connect_timeout=5.0, rpc_timeout=5.0)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
        assert connection._process is not None
        assert connection._process.returncode is not None
        assert set(threading.enumerate()) == threads


def test_bootstrap_without_site_packages_or_sync_runtime(isolated_connection: Connection) -> None:
    assert isolated_connection(TransportChecks.interpreter) == (True, True, False, False)


@pytest.mark.parametrize("size", [64, 2 * 1024 * 1024])
@pytest.mark.parametrize("method", [TransportChecks.echo, TransportChecks.async_echo])
def test_transport_roundtrip_small_and_compressed_packets(isolated_connection, size, method):
    payload = b"rmote\x00\xff" * (size // 7) + b"x" * (size % 7)
    assert isolated_connection(method, payload) == payload
    assert isolated_connection(TransportChecks.echo, b"after large packet") == b"after large packet"
