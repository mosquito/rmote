"""Execute client documentation against real subprocess and SSH transports."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.ssh_agent import agent as agent


@pytest.fixture
def docs_ssh_agent(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run forwarding examples with an isolated agent, never the user's agent."""
    isolated_agent = request.getfixturevalue("agent")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(isolated_agent[0]))


@pytest.fixture
def tool_examples(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    directory = Path(__file__).resolve().parents[1] / "examples" / "tools"
    monkeypatch.syspath_prepend(str(directory))
    yield
    for path in directory.glob("*.py"):
        sys.modules.pop(path.stem, None)


@pytest.fixture
def quickstart_container(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[str]:
    """Run the quickstart against a disposable Debian host with real systemd."""
    import os
    import shutil
    import subprocess
    import time
    import uuid

    if request.config.getoption("--no-docker", default=False):
        pytest.skip("Docker tests disabled with --no-docker")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker not available")

    key = tmp_path / "quickstart_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=10)
    monkeypatch.setenv("RMOTE_SSH_PUBLIC_KEY", str(key.with_suffix(".pub")))

    examples = Path(__file__).resolve().parents[1] / "examples" / "quickstart"
    image = "rmote-quickstart:test"
    build = [docker, "build", "-t", image]
    if base_image := os.environ.get("RMOTE_QUICKSTART_BASE_IMAGE"):
        build += ["--build-arg", f"BASE_IMAGE={base_image}"]
    subprocess.run([*build, str(examples)], check=True, timeout=240)
    container = f"rmote-quickstart-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("RMOTE_CONTAINER", container)
    monkeypatch.syspath_prepend(str(examples))
    try:
        subprocess.run(
            [
                docker,
                "run",
                "-d",
                "--rm",
                "--name",
                container,
                "--privileged",
                "--cgroupns=private",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                image,
            ],
            check=True,
            timeout=30,
        )
        # Docker can accept exec before systemd creates its bus socket.
        # Poll without --wait so early boot states also use the same deadline.
        deadline = time.monotonic() + 30
        while True:
            boot = subprocess.run(
                [docker, "exec", container, "systemctl", "is-system-running"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Container-only unit failures can mark a usable host degraded.
            if boot.stdout.strip() in {"running", "degraded"}:
                break
            if time.monotonic() >= deadline:
                logs = subprocess.run([docker, "logs", container], capture_output=True, text=True, timeout=5)
                pytest.fail(
                    "Container systemd did not finish booting within 30 seconds:\n"
                    + boot.stdout
                    + boot.stderr
                    + logs.stdout
                    + logs.stderr
                )
            time.sleep(0.1)
        yield container
    finally:
        sys.modules.pop("cache_tools", None)
        subprocess.run([docker, "rm", "-f", container], check=True, timeout=30)
