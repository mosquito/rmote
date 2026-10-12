"""Shared transport fixtures, explicitly imported by each test area's conftest."""

import asyncio
import shutil
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio

from rmote.protocol import Protocol, TLSConfig, TLSStream


@asynccontextmanager
async def subprocess_protocol(
    *command: str, cwd: Path | None = None, tls: TLSConfig | None = None
) -> AsyncGenerator[Protocol, None]:
    """Always reap the peer, including failed setup and exceptions in tests."""
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        proto = await Protocol.from_subprocess(process, tls=tls)
        async with proto:
            assert isinstance(proto.writer, TLSStream) is (tls is not None)
            yield proto
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        await process.wait()


@pytest_asyncio.fixture
async def protocol(transport_tls: TLSConfig | None):
    async with subprocess_protocol(sys.executable, "-qui", tls=transport_tls) as remote:
        yield remote


@pytest.fixture
def docker(request: pytest.FixtureRequest) -> str:
    if request.config.getoption("--no-docker"):
        pytest.skip("Docker tests disabled with --no-docker")
    path = shutil.which("docker")
    if path is None:
        pytest.skip("docker not available")
    return path


@pytest.fixture
def docker_image() -> str:
    return "python:3-slim"


@pytest_asyncio.fixture
async def docker_protocol(docker_image: str, docker: str, transport_tls: TLSConfig | None):
    async with subprocess_protocol(
        docker, "run", "--rm", "-i", docker_image, "python3", "-qui", tls=transport_tls
    ) as remote:
        yield remote


@pytest_asyncio.fixture
async def pacman_docker_protocol(docker: str, transport_tls: TLSConfig | None) -> AsyncGenerator[Protocol, None]:
    build = await asyncio.create_subprocess_exec(
        docker,
        "build",
        "--platform",
        "linux/amd64",
        "-t",
        "archlinux:python",
        ".",
        cwd=Path(__file__).parents[1] / "tools" / "docker" / "archlinux",
    )
    if await build.wait():
        raise RuntimeError("Failed to build Arch test image")

    async with subprocess_protocol(
        docker,
        "run",
        "--platform",
        "linux/amd64",
        "--rm",
        "-i",
        "archlinux:python",
        "python3",
        "-qui",
        tls=transport_tls,
    ) as remote:
        yield remote
