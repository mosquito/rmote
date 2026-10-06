import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class Server:
    process: subprocess.Popen[bytes]
    path: Path
    ssh: str

    def command(self, *args: str) -> list[str]:
        # No accidental network connection if a mux request is rejected.
        return [self.ssh, "-F", "/dev/null", "-S", str(self.path), "-o", "ProxyCommand=false", *args]

    def run(self, *args: str, input: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(self.command(*args), input=input, capture_output=True, timeout=15)


@pytest.fixture
def server(request: pytest.FixtureRequest):
    ssh = shutil.which("ssh")
    if os.name != "posix" or ssh is None:
        pytest.skip("requires POSIX and an OpenSSH client")
    # macOS AF_UNIX paths must fit in 104 bytes; pytest tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="rmux-", dir="/tmp") as directory:
        path = Path(directory) / "s"
        command = getattr(request, "param", [sys.executable, "-m", "rmote"])
        process = subprocess.Popen(
            [*command, "sshmux", "--socket", str(path), "--python", sys.executable],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, SHELL="/bin/sh"),
        )
        instance = Server(process, path, ssh)
        try:
            deadline = time.monotonic() + 10
            while not path.exists():
                if process.poll() is not None:
                    pytest.fail(f"mux server exited: {process.communicate()!r}")
                assert time.monotonic() < deadline, "mux server did not start"
                time.sleep(0.01)
            assert instance.run("-O", "check", "host").returncode == 0
            yield instance
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
