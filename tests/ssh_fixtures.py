"""Isolated SSH server fixture shared by transport and documentation tests."""

import getpass
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def local_sshd(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[int, str, str]]:
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    keygen = shutil.which("ssh-keygen")
    if not os.path.isfile(sshd) or keygen is None or shutil.which("ssh") is None:
        message = "sshd, ssh, and ssh-keygen are required for local SSH integration"
        if request.config.getoption("--require-ssh", default=False):
            pytest.fail(message)
        pytest.skip(message)
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    for key in [host_key, client_key]:
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=5)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "sshd_config"
    config.write_text(
        f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {host_key}\n"
        f"AuthorizedKeysFile {client_key}.pub\nPidFile {tmp_path / 'sshd.pid'}\n"
        "StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "UsePAM no\nLogLevel ERROR\n"
        f"AllowUsers {getpass.getuser()}\n"
    )
    error_log = tmp_path / "sshd.log"
    with error_log.open("w") as stderr:
        server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stderr=stderr, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while True:
                if server.poll() is not None:
                    pytest.fail(f"Isolated sshd startup failed: {error_log.read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Isolated sshd did not start")
                    time.sleep(0.01)
            yield port, str(client_key), getpass.getuser()
        finally:
            try:
                os.killpg(server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)
