import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.cli.sshmux.conftest import Server


@pytest.mark.parametrize(
    "server",
    [[sys.executable, "-m", "rmote"], [str(Path(sys.executable).with_name("rmote"))]],
    indirect=True,
    ids=["module", "script"],
)
def test_exec_preserves_output_streams_and_exit_status(server: Server):
    result = server.run("host", "printf stdout; printf stderr >&2; exit 7")
    assert (result.returncode, result.stdout, result.stderr) == (7, b"stdout", b"stderr")


def test_binary_input_and_half_close(server: Server):
    payload = bytes(range(256)) * 4096
    result = server.run("host", "cat; printf DONE", input=payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout == payload + b"DONE"


def test_stderr_larger_than_pipe_capacity_is_drained(server: Server):
    code = "import os; os.write(2,b'e'*300000); os.write(1,b'done')"
    result = server.run("host", f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")
    assert (result.returncode, result.stdout, result.stderr) == (0, b"done", b"e" * 300000)


def test_null_input_and_regular_output_file(server: Server, tmp_path):
    target = tmp_path / "out"
    with target.open("wb") as stream:
        result = subprocess.run(
            server.command("-n", "host", "cat; printf DONE"),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b"DONE"


def test_setenv_reaches_the_remote_command(server: Server):
    result = server.run("-o", "SetEnv=RMOTE_MUX_TEST=value", "host", 'printf "%s" "$RMOTE_MUX_TEST"')
    assert (result.returncode, result.stdout) == (0, b"value")


def test_multiple_live_sessions_are_independent(server: Server):
    parent = server.run("host", 'printf "%s\\n" "$PPID"').stdout
    children = [
        subprocess.Popen(
            server.command("host", "echo $PPID; cat"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    try:
        # All three keep stdin open. A fourth command must finish meanwhile.
        assert server.run("host", "printf fourth").stdout == b"fourth"
        for index, child in enumerate(children):
            marker = f"session-{index}\n".encode()
            out, err = child.communicate(marker, timeout=10)
            assert (child.returncode, out, err) == (0, parent + marker, b"")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate()


@pytest.mark.parametrize("nested", [False, True], ids=["exec", "child"])
def test_disconnect_reaps_its_vty_and_keeps_server_usable(server: Server, tmp_path, nested: bool):
    pidfile = tmp_path / "pid"
    target = shlex.quote(str(pidfile))
    command = f"sleep 300 & echo $! > {target}; wait" if nested else f"echo $$ > {target}; exec sleep 300"
    child = subprocess.Popen(
        server.command("host", command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        remote_pid = int(pidfile.read_text())
        child.terminate()
        child.communicate(timeout=10)
        deadline = time.monotonic() + 10
        while True:
            try:
                os.kill(remote_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "Vty child outlived its disconnected ssh client"
            time.sleep(0.01)
        assert server.run("host", "printf alive").stdout == b"alive"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


def test_control_exit_removes_socket(server: Server):
    result = server.run("-O", "exit", "host")
    assert result.returncode == 0, result.stderr
    assert server.process.wait(timeout=10) == 0
    assert not server.path.exists()


def test_sigterm_closes_server_and_active_clients(server: Server):
    child = subprocess.Popen(
        server.command("host", "cat"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        server.process.send_signal(signal.SIGTERM)
        server.process.wait(timeout=10)
        child.communicate(timeout=10)
        assert not server.path.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


def test_subsystem_is_explicitly_rejected(server: Server):
    result = server.run("-s", "host", "sftp")
    assert result.returncode != 0
    assert b"Subsystems are not supported" in result.stderr


@pytest.mark.parametrize(
    ("options", "warning"),
    [("-A", b"agent forwarding"), ("-X", b"X11 forwarding"), ("-AX", b"agent and X11 forwarding")],
    ids=["agent", "x11", "both"],
)
def test_optional_forwarding_does_not_block_the_session(server: Server, options: str, warning: bytes):
    result = server.run(options, "host", "printf connected; exit 7")
    assert result.returncode == 7
    assert result.stdout == b"connected"
    assert warning in result.stderr
    assert b"continuing without forwarding" in result.stderr
    assert b"session request failed" not in result.stderr


def test_forwarding_is_explicitly_rejected(server: Server):
    result = server.run("-O", "forward", "-L", "12345:localhost:54321", "host")
    assert result.returncode != 0
    assert b"supports shell and exec sessions only" in result.stderr
