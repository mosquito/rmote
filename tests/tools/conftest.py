import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio

from rmote.protocol import Protocol

_PLATFORM = "linux/amd64"
_IMAGE_TAG = "archlinux:python"


@pytest_asyncio.fixture
async def pacman_docker_protocol(docker: str) -> AsyncGenerator[Protocol, None]:
    build = await asyncio.create_subprocess_exec(
        docker,
        "build",
        "--platform",
        _PLATFORM,
        "-t",
        _IMAGE_TAG,
        ".",
        cwd=Path(__file__).parent / "docker" / "archlinux",
    )
    await build.wait()

    process = await asyncio.create_subprocess_exec(
        docker,
        "run",
        "--platform",
        _PLATFORM,
        "--rm",
        "-i",
        _IMAGE_TAG,
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
