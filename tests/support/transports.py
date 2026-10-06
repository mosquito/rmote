"""Shared transport fixtures, explicitly imported by each test area's conftest."""

import asyncio
import shutil
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio

from rmote.protocol import Protocol


@pytest_asyncio.fixture
async def protocol():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proto = await Protocol.from_subprocess(process)
    async with proto:
        yield proto
    try:
        process.terminate()
    except ProcessLookupError:
        # Closing the protocol may already have let the interpreter exit.
        pass
    await process.wait()


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
async def docker_protocol(docker_image: str, docker: str):
    process = await asyncio.create_subprocess_exec(
        docker,
        "run",
        "--rm",
        "-i",
        docker_image,
        "python3",
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proto = await Protocol.from_subprocess(process)
    async with proto:
        yield proto
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


@pytest_asyncio.fixture
async def pacman_docker_protocol(docker: str) -> AsyncGenerator[Protocol, None]:
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

    process = await asyncio.create_subprocess_exec(
        docker,
        "run",
        "--platform",
        "linux/amd64",
        "--rm",
        "-i",
        "archlinux:python",
        "python3",
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proto = await Protocol.from_subprocess(process)
    async with proto:
        yield proto
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()
