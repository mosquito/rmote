"""Agent listing and signatures through real OpenSSH mux clients."""

import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.cli.sshmux.conftest import Server


@pytest.fixture
def server_options(request, agent):
    return ["--agent-socket", str(agent[0])] if getattr(request, "param", False) else []


@pytest.fixture
def server_env(agent, server_options):
    path, _, _ = agent
    if server_options:
        path = "/does-not-exist/ignored-agent"
    return dict(os.environ, SHELL="/bin/sh", SSH_AUTH_SOCK=str(path), SSH_AGENT_PID="")


def wait_removed(path: Path) -> None:
    deadline = time.monotonic() + 5
    while path.exists() or path.parent.exists():
        assert time.monotonic() < deadline, f"agent socket outlived session: {path}"
        time.sleep(0.01)


@pytest.mark.parametrize("option", ["-A", "-AX"])
@pytest.mark.parametrize("server_options", [False, True], indirect=True, ids=["env", "explicit-socket"])
def test_agent_lists_keys_and_signs_through_forwarded_socket(server: Server, agent, option, server_options):
    path, public, _ = agent
    result = server.run(
        option, "host", f'printf "%s\\n" "$SSH_AUTH_SOCK"; ssh-add -L; ssh-add -T {shlex.quote(str(public))}'
    )
    assert result.returncode == 0, result.stderr
    remote_path, listed = result.stdout.split(b"\n", 1)
    assert listed.strip() == public.read_bytes().strip()
    remote = Path(os.fsdecode(remote_path))
    assert remote != path
    assert remote.parent.name.startswith("rmote-agent-")
    wait_removed(remote)
    assert b"agent forwarding unavailable" not in result.stderr
    if option == "-AX":
        assert b"without X11 forwarding" in result.stderr


def test_agent_is_not_inherited_without_forwarding_or_via_setenv(server: Server, agent):
    path, _, _ = agent
    for options in ([], ["-a"], ["-a", "-o", f"SetEnv=SSH_AUTH_SOCK={path}"]):
        result = server.run(*options, "host", 'printf "%s" "$SSH_AUTH_SOCK"')
        assert (result.returncode, result.stdout) == (0, b"")


def test_agent_has_private_paths_for_parallel_sessions_and_connections(server: Server, agent):
    _, public, _ = agent
    command = f"ssh-add -T {shlex.quote(str(public))}"

    def sign(index):
        result = server.run("-A", "host", f'printf "%s\\n" "$SSH_AUTH_SOCK"; {command} & p=$!; {command}; wait "$p"')
        assert result.returncode == 0, result.stderr
        path = Path(os.fsdecode(result.stdout.strip()))
        wait_removed(path)
        return path

    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(sign, range(3)))
    assert len(set(paths)) == 3
    assert server.run("host", "printf alive").stdout == b"alive"


def test_disconnect_removes_agent_socket_and_preserves_other_session(server: Server, agent):
    _, public, _ = agent
    child = subprocess.Popen(
        server.command("-A", "host", 'printf "%s\\n" "$SSH_AUTH_SOCK"; cat'),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        path = Path(os.fsdecode(child.stdout.readline().strip()))
        assert path.is_socket()
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        child.terminate()
        child.communicate(timeout=5)
        wait_removed(path)
        result = server.run("-A", "host", f"ssh-add -T {shlex.quote(str(public))}")
        assert result.returncode == 0, result.stderr
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


def test_dead_agent_warns_without_breaking_shell(server: Server, agent):
    _, _, process = agent
    process.terminate()
    process.wait(timeout=5)
    result = server.run("-A", "host", 'printf "connected:%s" "$SSH_AUTH_SOCK"; exit 7')
    assert (result.returncode, result.stdout) == (7, b"connected:")
    assert b"agent forwarding unavailable" in result.stderr


def test_shell_failure_releases_agent_listener(server: Server):
    result = server.run("-A", "host", 'printf "%s\\n" "$SSH_AUTH_SOCK"; exit 19')
    assert result.returncode == 19
    wait_removed(Path(os.fsdecode(result.stdout.strip())))
