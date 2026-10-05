"""Fixtures for protocol, serialization and remote Tool tests."""

import asyncio
import sys

import pytest_asyncio

from rmote.protocol import Protocol
from tests.support.transports import docker as docker
from tests.support.transports import docker_image as docker_image
from tests.support.transports import docker_protocol as docker_protocol
from tests.support.transports import protocol as protocol


@pytest_asyncio.fixture
async def isolated_remote(tmp_path):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-qui",
        cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with await Protocol.from_subprocess(child) as remote:
            yield remote
    finally:
        if child.returncode is None:
            child.terminate()
        await child.wait()
