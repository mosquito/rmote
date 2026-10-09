"""MOTD comes from remote files and cannot contaminate exec or SFTP streams."""

import os
import subprocess
import sys

import pytest

from tests.cli.sshmux.conftest import Server
from tests.cli.sshmux.test_sftp import sftp

# Override the class paths. The real launcher runs in the remote subprocess;
# no test needs to change /etc or the user's login files.
ENTRYPOINT = """
from pathlib import Path
from rmote.cli import main, sshmux
sshmux.MuxServer.motd_paths = (Path.home() / 'dynamic', Path.home() / 'static')
raise SystemExit(main())
"""

pytestmark = pytest.mark.parametrize("server", [[sys.executable, "-c", ENTRYPOINT]], indirect=True)


@pytest.fixture
def server_env(tmp_path):
    (tmp_path / "dynamic").write_bytes(b"DYNAMIC-MOTD\n")
    (tmp_path / "static").write_bytes(b"STATIC-MOTD\n")
    shell = tmp_path / "shell"
    shell.write_text('#!/bin/sh\nif [ "$1" = "-l" ]; then printf "LOGIN-SHELL\\n"; else exec /bin/sh "$@"; fi\n')
    shell.chmod(0o700)
    return dict(os.environ, HOME=str(tmp_path), SHELL=str(shell), SSH_AUTH_SOCK="", SSH_AGENT_PID="")


@pytest.fixture
def server_options(request):
    return ["--motd"] if getattr(request, "param", True) else []


def terminal(server: Server, *command: str) -> subprocess.CompletedProcess[bytes]:
    # Keep stdin open until the short fixture shell exits. Closing it early
    # sends VEOF, which macOS can echo as ^D before the shell starts.
    with subprocess.Popen(
        server.command("-tt", "host", *command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            process.wait(timeout=10)
            output, error = process.communicate()
            return subprocess.CompletedProcess(process.args, process.returncode, output, error)
        finally:
            if process.poll() is None:
                process.kill()


@pytest.mark.parametrize("server_options", [False, True], indirect=True)
def test_motd_precedes_each_interactive_shell_only_when_enabled(server: Server, server_options):
    expected = b"DYNAMIC-MOTD\r\nSTATIC-MOTD\r\n" if server_options else b""
    for _ in range(2):
        result = terminal(server)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected + b"LOGIN-SHELL\r\n"


def test_hushlogin_suppresses_motd(server: Server, tmp_path):
    (tmp_path / ".hushlogin").touch()
    result = terminal(server)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"LOGIN-SHELL\r\n"


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "unreadable", "same-file"])
def test_unavailable_or_duplicate_motd_does_not_prevent_login(server: Server, tmp_path, kind):
    dynamic = tmp_path / "dynamic"
    dynamic.unlink()
    if kind == "directory":
        dynamic.mkdir()
    elif kind == "fifo":
        os.mkfifo(dynamic)
    elif kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root can read mode 000 files")
        dynamic.write_bytes(b"PRIVATE-MOTD\n")
        dynamic.chmod(0)
    elif kind == "same-file":
        dynamic.symlink_to(tmp_path / "static")
    result = terminal(server)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"STATIC-MOTD\r\nLOGIN-SHELL\r\n"


def test_motd_does_not_change_commands_or_shells_without_a_pty(server: Server):
    result = server.run("host", "printf stdout; printf stderr >&2; exit 7")
    assert (result.returncode, result.stdout, result.stderr) == (7, b"stdout", b"stderr")
    result = terminal(server, "printf command; exit 7")
    assert (result.returncode, result.stdout) == (7, b"command")
    result = server.run("-T", "host")
    assert (result.returncode, result.stdout, result.stderr) == (0, b"LOGIN-SHELL\n", b"")


def test_sftp_transfers_binary_files_with_motd_enabled(server: Server, tmp_path):
    source, copy = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(bytes(range(256)) * 100)
    result = sftp(server, f'get "{source}" "{copy}"\nbye\n')
    assert result.returncode == 0, result.stderr
    assert copy.read_bytes() == source.read_bytes()
    assert b"MOTD" not in result.stdout + result.stderr
