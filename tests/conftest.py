import asyncio
import importlib
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.support.logging import Capture
from tests.support.synchronization import Deadline, Fifo

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


@pytest.fixture
def fifo_factory(tmp_path: Path):
    pipes: list[Fifo] = []

    def create() -> Fifo:
        pipe = Fifo(tmp_path / f"signal-{len(pipes)}.fifo")
        pipes.append(pipe)
        return pipe

    try:
        yield create
    finally:
        for pipe in pipes:
            pipe.close()


@pytest.fixture
def fifo(fifo_factory: Callable[[], Fifo]) -> Fifo:
    """Release a remote wait, or receive its readiness notification."""
    return fifo_factory()


@pytest.fixture
def deadline(monkeypatch):
    """Control a module's deadline without changing pytest's event loop clock."""

    def install(module: str, seconds: float = 123.0) -> Deadline:
        controlled = Deadline(seconds)

        class Asyncio:
            timeout = controlled
            wait_for = staticmethod(controlled.wait_for)

            def __getattr__(self, name):
                return getattr(asyncio, name)

        monkeypatch.setattr(importlib.import_module(module), "asyncio", Asyncio())
        return controlled

    return install


@pytest.fixture
def capture_logs():
    attached = []

    def attach(name: str) -> Capture:
        logger = logging.getLogger(name)
        handler = Capture()
        attached.append((logger, handler, logger.level))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        return handler

    try:
        yield attach
    finally:
        for logger, handler, level in reversed(attached):
            logger.removeHandler(handler)
            logger.setLevel(level)
            handler.close()
