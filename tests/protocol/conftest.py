"""Fixtures for protocol, serialization and remote Tool tests."""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest_asyncio

from rmote.protocol import Protocol
from tests.support.transports import docker as docker
from tests.support.transports import docker_image as docker_image
from tests.support.transports import docker_protocol as docker_protocol
from tests.support.transports import protocol as protocol
from tests.support.transports import subprocess_protocol


@pytest_asyncio.fixture
async def client():
    """A protocol without a peer for focused protocol state tests."""
    return Protocol(asyncio.StreamReader(), MagicMock(spec=asyncio.StreamWriter))


@pytest_asyncio.fixture
async def isolated_remote(tmp_path):
    async with subprocess_protocol(sys.executable, "-I", "-S", "-qui", cwd=tmp_path) as remote:
        yield remote
