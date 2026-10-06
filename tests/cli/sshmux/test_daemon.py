import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from rmote.cli.sshmux import running_pid


def wait_removed(path: Path) -> None:
    deadline = time.monotonic() + 5
    while path.exists():
        assert time.monotonic() < deadline, "daemon did not remove its socket"
        time.sleep(0.02)


@dataclass
class Daemon:
    path: Path
    ssh: str

    def command(self, *args: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "rmote",
            "sshmux",
            "--daemon",
            "--socket",
            str(self.path),
            "--python",
            sys.executable,
            *args,
        ]

    def start(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(self.command(*args), capture_output=True, timeout=10)

    def client(self, *args: str) -> list[str]:
        return [self.ssh, "-F", "/dev/null", "-S", str(self.path), "-o", "ProxyCommand=false", *args]

    def stop(self) -> None:
        subprocess.run(self.client("-O", "exit", "host"), capture_output=True, timeout=5)
        wait_removed(self.path)


@pytest.fixture
def daemon():
    ssh = shutil.which("ssh")
    if ssh is None:
        pytest.skip("requires OpenSSH")
    with tempfile.TemporaryDirectory(prefix="rmux-d-", dir="/tmp") as directory:
        instance = Daemon(Path(directory) / "s", ssh)
        try:
            yield instance
        finally:
            if instance.path.exists() and stat.S_ISSOCK(instance.path.lstat().st_mode):
                instance.stop()


def test_starts_detached_and_reuses_existing_server(daemon: Daemon):
    result = daemon.start()
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    pid = running_pid(str(daemon.path))
    assert pid is not None
    assert os.getsid(pid) != os.getsid(0)
    assert os.getsid(pid) != pid  # double fork: no controlling terminal can be acquired
    for suffix in ("", ".log", ".lock"):
        assert stat.S_IMODE(Path(str(daemon.path) + suffix).stat().st_mode) == 0o600
    # A live socket is authoritative; a second transport must not run.
    result = daemon.start("--", "/nonexistent-rmote-transport")
    assert result.returncode == 0, result.stderr
    assert running_pid(str(daemon.path)) == pid
    result = subprocess.run(
        daemon.client("host", "printf stdout; printf stderr >&2; exit 7"), capture_output=True, timeout=10
    )
    assert (result.returncode, result.stdout, result.stderr) == (7, b"stdout", b"stderr")


def test_concurrent_starts_open_only_one_transport(daemon: Daemon):
    marker = daemon.path.with_name("starts")
    helper = daemon.path.with_name("transport.py")
    helper.write_text(
        "import os, sys\n"
        f"with open({str(marker)!r}, 'a') as stream: stream.write('start\\n')\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    processes = [
        subprocess.Popen(
            daemon.command("--", sys.executable, str(helper)), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(4)
    ]
    try:
        for process in processes:
            output, error = process.communicate(timeout=10)
            assert (process.returncode, output, error) == (0, b"", b"")
        assert marker.read_text().splitlines() == ["start"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


def test_ssh_config_starts_reuses_and_restarts_server(daemon: Daemon):
    config = daemon.path.with_name("ssh_config")
    launcher = [
        str(Path(sys.executable).with_name("rmote")),
        *daemon.command()[3:],
        "--",
        "env",
        "RMOTE_CONFIG=connected",
    ]
    command = shlex.join(launcher).replace('"', '\\"')
    config.write_text(
        f"Host container\n    ControlPath {daemon.path}\n    ControlMaster no\n"
        "    ProxyCommand false\n    ForwardAgent no\n    ForwardX11 no\n"
        f'Match originalhost container exec "{command}"\nMatch all\n'
    )
    client = [daemon.ssh, "-F", str(config), "container"]
    result = subprocess.run([*client, 'printf "%s" "$RMOTE_CONFIG"; exit 7'], capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (7, b"connected", b"")
    pid = running_pid(str(daemon.path))
    payload = bytes(range(256))
    result = subprocess.run([*client, "cat"], input=payload, capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, payload, b"")
    assert running_pid(str(daemon.path)) == pid
    daemon.stop()
    result = subprocess.run([*client, "printf restarted"], capture_output=True, timeout=10)
    assert (result.returncode, result.stdout) == (0, b"restarted")
    assert running_pid(str(daemon.path)) != pid


def test_transport_failure_is_reported_and_lock_is_released(daemon: Daemon):
    result = daemon.start("--", "/nonexistent-rmote-transport")
    assert result.returncode == 1
    assert b"nonexistent-rmote-transport" in result.stderr
    assert result.stdout == b""
    assert not daemon.path.exists()
    result = daemon.start()
    assert result.returncode == 0, result.stderr


def test_refused_socket_is_recovered(daemon: Daemon):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(daemon.path))
    result = daemon.start()
    assert result.returncode == 0, result.stderr
    assert running_pid(str(daemon.path)) is not None


def test_existing_file_is_preserved(daemon: Daemon):
    daemon.path.write_text("keep")
    result = daemon.start()
    assert result.returncode == 1
    assert daemon.path.read_text() == "keep"


def test_idle_timeout_without_any_session(daemon: Daemon):
    result = daemon.start("--idle-timeout", "0.2")
    assert result.returncode == 0, result.stderr
    wait_removed(daemon.path)


@pytest.mark.parametrize("value", ["-1", "nan", "inf"], ids=["negative", "nan", "infinite"])
def test_invalid_idle_timeout(daemon: Daemon, value: str):
    result = daemon.start(f"--idle-timeout={value}")
    assert result.returncode == 2
    assert not daemon.path.exists()


def test_startup_timeout_releases_lock(daemon: Daemon):
    helper = daemon.path.with_name("slow.py")
    helper.write_text("import threading\nthreading.Event().wait()\n")
    code = "import sys; import rmote.cli.sshmux as d; d.START_TIMEOUT = 0.2; from rmote.cli import main; sys.exit(main(sys.argv[1:]))"
    result = subprocess.run(
        [sys.executable, "-c", code, *daemon.command()[3:], "--", sys.executable, str(helper)],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert b"Timed out starting" in result.stderr
    assert not daemon.path.exists()
    result = daemon.start()
    assert result.returncode == 0, result.stderr
