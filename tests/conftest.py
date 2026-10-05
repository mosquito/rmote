import asyncio
import logging

import pytest

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
