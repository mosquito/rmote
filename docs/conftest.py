"""Execute client documentation against real subprocess and SSH transports."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


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
        boot = subprocess.run(
            [docker, "exec", container, "systemctl", "is-system-running", "--wait"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Container-only unit failures can mark the host degraded; systemd must
        # nevertheless finish booting and accept real service operations.
        assert boot.stdout.strip() in {"running", "degraded"}, boot.stdout + boot.stderr
        yield container
    finally:
        sys.modules.pop("cache_tools", None)
        subprocess.run([docker, "rm", "-f", container], check=True, timeout=30)
