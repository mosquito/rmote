"""Integration tests for Pacman tool - require docker with archlinux:latest."""

import pytest

from rmote.protocol import Protocol
from rmote.tools.pacman import Package, Pacman, State

pytestmark = pytest.mark.timeout(120)


@pytest.mark.asyncio
async def test_pacman_update(pacman_docker_protocol: Protocol) -> None:
    result = await pacman_docker_protocol(Pacman.update)
    assert result is True


@pytest.mark.asyncio
async def test_pacman_update_ttl(pacman_docker_protocol: Protocol, subtests) -> None:
    with subtests.test("ttl=-1 always runs"):
        result = await pacman_docker_protocol(Pacman.update, ttl=-1)
        assert result is True

    with subtests.test("ttl=3600 skips after recent update"):
        result = await pacman_docker_protocol(Pacman.update, ttl=3600)
        assert result is False

    with subtests.test("ttl=0 always runs"):
        result = await pacman_docker_protocol(Pacman.update, ttl=0)
        assert result is True


@pytest.mark.asyncio
async def test_pacman_install_present(pacman_docker_protocol: Protocol) -> None:
    result = await pacman_docker_protocol(Pacman.package, "nano", State.PRESENT)
    assert result.changed is True
    assert result.version != ""

    # idempotent
    result = await pacman_docker_protocol(Pacman.package, "nano", State.PRESENT)
    assert result.changed is False
    assert result.version != ""


@pytest.mark.asyncio
async def test_pacman_remove_absent(pacman_docker_protocol: Protocol) -> None:
    await pacman_docker_protocol(Pacman.package, "nano", State.PRESENT)

    result = await pacman_docker_protocol(Pacman.package, "nano", State.ABSENT)
    assert result.changed is True
    assert result.version == ""

    # idempotent
    result = await pacman_docker_protocol(Pacman.package, "nano", State.ABSENT)
    assert result.changed is False
    assert result.version == ""


@pytest.mark.asyncio
async def test_pacman_already_absent(pacman_docker_protocol: Protocol) -> None:
    result = await pacman_docker_protocol(Pacman.package, "nano", State.ABSENT)
    assert result.changed is False
    assert result.version == ""


@pytest.mark.asyncio
async def test_pacman_converge(pacman_docker_protocol: Protocol) -> None:
    packages = [Package("nano"), Package("tree")]
    results = await pacman_docker_protocol(Pacman.converge, *packages)

    assert len(results) == 2
    assert all(r.changed is True for r in results)
    assert all(r.version != "" for r in results)

    # idempotent
    results = await pacman_docker_protocol(Pacman.converge, *packages)
    assert all(r.changed is False for r in results)


@pytest.mark.asyncio
async def test_pacman_latest(pacman_docker_protocol: Protocol) -> None:
    # not installed → LATEST installs it
    result = await pacman_docker_protocol(Pacman.package, "nano", State.LATEST)
    assert result.changed is True
    assert result.version != ""

    # already up to date → no change
    result = await pacman_docker_protocol(Pacman.package, "nano", State.LATEST)
    assert result.changed is False
