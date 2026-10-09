"""An isolated OpenSSH agent with one temporary signing key."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def agent(tmp_path):
    for program in ("ssh-agent", "ssh-add", "ssh-keygen"):
        if shutil.which(program) is None:
            pytest.skip(f"requires {program}")
    with tempfile.TemporaryDirectory(prefix="rmote-test-agent-", dir="/tmp") as directory:
        path = Path(directory) / "a"
        process = subprocess.Popen(
            ["ssh-agent", "-D", "-a", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        env = dict(os.environ, SSH_AUTH_SOCK=str(path), SSH_ASKPASS_REQUIRE="never")
        key = tmp_path / "agent-key"
        try:
            deadline = time.monotonic() + 5
            while not path.exists():
                assert process.poll() is None, process.communicate()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(["ssh-add", str(key)], env=env, check=True, capture_output=True, timeout=5)
            yield path, key.with_suffix(".pub"), process
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
