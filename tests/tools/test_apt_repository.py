"""Integration tests for AptRepository tool - require docker."""

import pytest

from rmote.protocol import Protocol
from rmote.tools.apt_repository import AptRepository
from rmote.tools.fs import FileSystem

pytestmark = pytest.mark.timeout(60)


@pytest.mark.asyncio
async def test_apt_repository_present(docker_protocol: Protocol, subtests) -> None:
    with subtests.test("adds new repository"):
        result = await docker_protocol(
            AptRepository.present,
            "test-repo",
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main"],
        )
        assert result.changed is True

    with subtests.test("idempotent on second call"):
        result = await docker_protocol(
            AptRepository.present,
            "test-repo",
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main"],
        )
        assert result.changed is False

    with subtests.test("changed when content differs"):
        result = await docker_protocol(
            AptRepository.present,
            "test-repo",
            uris=["https://example.com/apt"],
            suites=["trixie"],
            components=["main"],
        )
        assert result.changed is True

    with subtests.test("result name matches"):
        result = await docker_protocol(
            AptRepository.present,
            "my-repo",
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main"],
        )
        assert result.name == "my-repo"


@pytest.mark.asyncio
async def test_apt_repository_deb822_format(docker_protocol: Protocol, subtests) -> None:
    with subtests.test("multiple types and components"):
        await docker_protocol(
            AptRepository.present,
            "fmt-repo",
            types=["deb", "deb-src"],
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main", "contrib"],
        )
        content = await docker_protocol(FileSystem.read_str, "/etc/apt/sources.list.d/fmt-repo.sources")
        assert "Types: deb deb-src" in content
        assert "Components: main contrib" in content

    with subtests.test("signed_by line present when set"):
        await docker_protocol(
            AptRepository.present,
            "signed-repo",
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main"],
            signed_by="/etc/apt/keyrings/example.asc",
        )
        content = await docker_protocol(FileSystem.read_str, "/etc/apt/sources.list.d/signed-repo.sources")
        assert "Signed-By: /etc/apt/keyrings/example.asc" in content

    with subtests.test("signed_by line absent when not set"):
        await docker_protocol(
            AptRepository.present,
            "unsigned-repo",
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main"],
        )
        content = await docker_protocol(FileSystem.read_str, "/etc/apt/sources.list.d/unsigned-repo.sources")
        assert "Signed-By" not in content


@pytest.mark.asyncio
async def test_apt_repository_absent(docker_protocol: Protocol, subtests) -> None:
    with subtests.test("removes existing .sources file"):
        await docker_protocol(
            AptRepository.present,
            "rm-repo",
            uris=["https://example.com/apt"],
            suites=["bookworm"],
            components=["main"],
        )
        result = await docker_protocol(AptRepository.absent, "rm-repo")
        assert result.changed is True

    with subtests.test("idempotent when already absent"):
        result = await docker_protocol(AptRepository.absent, "rm-repo")
        assert result.changed is False


@pytest.mark.asyncio
async def test_apt_repository_key(docker_protocol: Protocol, subtests) -> None:
    key_data = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake key\n-----END PGP PUBLIC KEY BLOCK-----\n"

    with subtests.test("installs key"):
        result = await docker_protocol(AptRepository.key, "test.asc", key_data)
        assert result.changed is True
        assert result.name == "test.asc"

    with subtests.test("idempotent with same key data"):
        result = await docker_protocol(AptRepository.key, "test.asc", key_data)
        assert result.changed is False

    with subtests.test("changed when key data differs"):
        result = await docker_protocol(AptRepository.key, "test.asc", b"different key")
        assert result.changed is True
