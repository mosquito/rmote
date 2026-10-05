"""Real RPC targets for system-tool docstrings; each example gets a fresh container."""

import asyncio
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from rmote.protocol import Protocol

ROOT = Path(__file__).resolve().parents[2]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--no-docker", action="store_true", help="Skip tests that require Docker.")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if isinstance(item, pytest.DoctestItem) and any(
            'getfixture("debian_tool")' in example.source or 'getfixture("arch_tool")' in example.source
            for example in item.dtest.examples
        ):
            item.add_marker(pytest.mark.docker)


@pytest.fixture(scope="session")
def tool_docker(request: pytest.FixtureRequest) -> str:
    if request.config.getoption("--no-docker", default=False):
        pytest.skip("Docker tests disabled with --no-docker")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker not available")
    subprocess.run([docker, "info"], check=True, capture_output=True, timeout=15)
    return docker


@pytest.fixture(scope="session")
def debian_tool_image(tool_docker: str) -> str:
    image = "rmote-tool-doctests:debian"
    subprocess.run([tool_docker, "build", "-t", image, str(ROOT / "examples/quickstart")], check=True, timeout=300)
    return image


@pytest.fixture(scope="session")
def arch_tool_image(tool_docker: str) -> str:
    image = "rmote-tool-doctests:arch"
    subprocess.run(
        [tool_docker, "build", "--platform", "linux/amd64", "-t", image, str(ROOT / "tests/tools/docker/archlinux")],
        check=True,
        timeout=300,
    )
    return image


def container_remote(docker: str, image: str, *, systemd: bool) -> Iterator[Callable[..., Any]]:
    name = "rmote-doctest-" + uuid.uuid4().hex[:12]
    # Keep failed containers until the finally block so their logs are available.
    command = [docker, "run", "-d", "--name", name]
    if systemd:
        # Private UTS/network namespaces isolate hostname and net.ipv4 sysctls.
        command += ["--privileged", "--cgroupns=private", "--tmpfs", "/run", "--tmpfs", "/run/lock"]
    else:
        command += ["--platform", "linux/amd64"]
    command += [image]
    if not systemd:
        command += ["sleep", "infinity"]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    try:
        if systemd:
            deadline = time.monotonic() + 30
            while True:
                state = subprocess.run(
                    [docker, "exec", name, "systemctl", "is-system-running"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                if state in {"running", "degraded"}:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Container systemd failed to boot: {state}")
                time.sleep(0.1)

        with tempfile.TemporaryFile() as stderr, asyncio.Runner() as runner:

            async def connect() -> tuple[asyncio.subprocess.Process, Protocol]:
                process = await asyncio.create_subprocess_exec(
                    docker,
                    "exec",
                    "-i",
                    name,
                    "python3",
                    "-qui",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr,
                )
                protocol = await Protocol.from_subprocess(process)
                await protocol.__aenter__()
                return process, protocol

            try:
                process, protocol = runner.run(connect())
            except Exception as exc:
                stderr.seek(0)
                exc.add_note(stderr.read().decode(errors="replace"))
                logs = subprocess.run([docker, "logs", name], capture_output=True, text=True, timeout=5)
                exc.add_note(logs.stdout + logs.stderr)
                raise
            try:

                def remote(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
                    async def call() -> Any:
                        async with asyncio.timeout(120):
                            return await protocol(function, *args, **kwargs)

                    return runner.run(call())

                yield remote
            finally:

                async def close() -> None:
                    await protocol.__aexit__(None, None, None)
                    if process.returncode is None:
                        process.terminate()
                    await process.wait()

                runner.run(close())
    finally:
        subprocess.run([docker, "rm", "-f", name], check=True, capture_output=True, timeout=30)


@pytest.fixture
def debian_tool(tool_docker: str, debian_tool_image: str) -> Iterator[Callable[..., Any]]:
    yield from container_remote(tool_docker, debian_tool_image, systemd=True)


@pytest.fixture
def arch_tool(tool_docker: str, arch_tool_image: str) -> Iterator[Callable[..., Any]]:
    yield from container_remote(tool_docker, arch_tool_image, systemd=False)
