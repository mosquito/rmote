"""Integration tests for Apt tool - require docker."""

import pytest

from rmote.protocol import Protocol
from rmote.tools.apt import Apt, Package, State

pytestmark = pytest.mark.timeout(120)


@pytest.mark.asyncio
async def test_apt_update(docker_protocol: Protocol) -> None:
    result = await docker_protocol(Apt.update)
    assert result is True


@pytest.mark.asyncio
async def test_apt_update_ttl(docker_protocol: Protocol, subtests) -> None:
    with subtests.test("ttl=-1 always runs"):
        result = await docker_protocol(Apt.update, ttl=-1)
        assert result is True

    with subtests.test("ttl=3600 skips after recent update"):
        result = await docker_protocol(Apt.update, ttl=3600)
        assert result is False

    with subtests.test("ttl=0 always runs"):
        result = await docker_protocol(Apt.update, ttl=0)
        assert result is True


@pytest.mark.asyncio
async def test_apt_install_present(docker_protocol: Protocol) -> None:
    await docker_protocol(Apt.update)

    result = await docker_protocol(Apt.package, "nano", State.PRESENT)
    assert result.changed is True
    assert result.version != ""

    # idempotent
    result = await docker_protocol(Apt.package, "nano", State.PRESENT)
    assert result.changed is False
    assert result.version != ""


@pytest.mark.asyncio
async def test_apt_remove_absent(docker_protocol: Protocol) -> None:
    await docker_protocol(Apt.update)
    await docker_protocol(Apt.package, "nano", State.PRESENT)

    result = await docker_protocol(Apt.package, "nano", State.ABSENT)
    assert result.changed is True
    assert result.version == ""

    # idempotent
    result = await docker_protocol(Apt.package, "nano", State.ABSENT)
    assert result.changed is False
    assert result.version == ""


@pytest.mark.asyncio
async def test_apt_already_absent(docker_protocol: Protocol) -> None:
    result = await docker_protocol(Apt.package, "nano", State.ABSENT)
    assert result.changed is False
    assert result.version == ""


@pytest.mark.asyncio
async def test_apt_converge(docker_protocol: Protocol) -> None:
    await docker_protocol(Apt.update)

    packages = [
        Package("nano"),
        Package("xxd"),
    ]
    results = await docker_protocol(Apt.converge, *packages)

    assert len(results) == 2
    assert all(r.changed is True for r in results)
    assert all(r.version != "" for r in results)

    # converge again - nothing changes
    results = await docker_protocol(Apt.converge, *packages)
    assert all(r.changed is False for r in results)


@pytest.mark.asyncio
async def test_apt_latest(docker_protocol: Protocol) -> None:
    await docker_protocol(Apt.update)

    # not installed → LATEST installs it
    result = await docker_protocol(Apt.package, "nano", State.LATEST)
    assert result.changed is True
    assert result.version != ""
