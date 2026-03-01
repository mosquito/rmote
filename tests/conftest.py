import asyncio
import logging
import shutil
import sys

import pytest
import pytest_asyncio

from rmote.protocol import Protocol

logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")


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
    process.terminate()
    await process.wait()


@pytest.fixture
def docker() -> str:
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
