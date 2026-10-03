import asyncio
import logging
import shutil
import sys

import pytest
import pytest_asyncio

from rmote.protocol import Protocol
from tests.ssh_fixtures import local_sshd as local_sshd

logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--require-ssh", action="store_true", help="Fail if the local SSH integration cannot run.")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "docker: requires Docker containers")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "docker" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.docker)


@pytest.fixture(scope="session", autouse=True)
def disable_implicit_event_loop() -> None:
    # Python 3.11 can create an unused loop when pytest-asyncio saves the policy.
    asyncio.set_event_loop(None)


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
