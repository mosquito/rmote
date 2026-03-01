"""Integration tests for PacmanRepository tool - require docker with archlinux:latest."""

import pytest

from rmote.protocol import Protocol
from rmote.tools.fs import FileSystem
from rmote.tools.pacman_repository import PacmanRepository

pytestmark = pytest.mark.timeout(60)


@pytest.mark.asyncio
async def test_pacman_repository_present(pacman_docker_protocol: Protocol, subtests) -> None:
    with subtests.test("adds new repository"):
        result = await pacman_docker_protocol(
            PacmanRepository.present,
            "testrepo",
            servers=["https://example.com/$repo/$arch"],
        )
        assert result.changed is True

    with subtests.test("idempotent on second call"):
        result = await pacman_docker_protocol(
            PacmanRepository.present,
            "testrepo",
            servers=["https://example.com/$repo/$arch"],
        )
        assert result.changed is False

    with subtests.test("changed when content differs"):
        result = await pacman_docker_protocol(
            PacmanRepository.present,
            "testrepo",
            servers=["https://mirror.example.com/$repo/$arch"],
        )
        assert result.changed is True

    with subtests.test("result name matches"):
        result = await pacman_docker_protocol(
            PacmanRepository.present,
            "myrepo",
            servers=["https://example.com/$repo/$arch"],
        )
        assert result.name == "myrepo"


@pytest.mark.asyncio
async def test_pacman_repository_format(pacman_docker_protocol: Protocol, subtests) -> None:
    with subtests.test("section header present"):
        await pacman_docker_protocol(
            PacmanRepository.present,
            "fmt-repo",
            servers=["https://example.com/$repo/$arch"],
            sig_level="Never",
        )
        content = await pacman_docker_protocol(FileSystem.read_str, "/etc/pacman.conf")
        assert "[fmt-repo]" in content
        assert "Server = https://example.com/$repo/$arch" in content
        assert "SigLevel = Never" in content

    with subtests.test("sig_level omitted when empty"):
        await pacman_docker_protocol(
            PacmanRepository.present,
            "nosig-repo",
            servers=["https://example.com/$repo/$arch"],
            sig_level="",
        )
        content = await pacman_docker_protocol(FileSystem.read_str, "/etc/pacman.conf")
        # find the nosig-repo section and check no SigLevel follows before next section
        idx = content.index("[nosig-repo]")
        section = content[idx:].split("\n[")[0]
        assert "SigLevel" not in section

    with subtests.test("multiple servers written"):
        await pacman_docker_protocol(
            PacmanRepository.present,
            "multi-repo",
            servers=["https://mirror1.example.com/$repo/$arch", "https://mirror2.example.com/$repo/$arch"],
        )
        content = await pacman_docker_protocol(FileSystem.read_str, "/etc/pacman.conf")
        assert "mirror1.example.com" in content
        assert "mirror2.example.com" in content


@pytest.mark.asyncio
async def test_pacman_repository_absent(pacman_docker_protocol: Protocol, subtests) -> None:
    with subtests.test("removes existing section"):
        await pacman_docker_protocol(
            PacmanRepository.present,
            "rm-repo",
            servers=["https://example.com/$repo/$arch"],
        )
        result = await pacman_docker_protocol(PacmanRepository.absent, "rm-repo")
        assert result.changed is True
        content = await pacman_docker_protocol(FileSystem.read_str, "/etc/pacman.conf")
        assert "[rm-repo]" not in content

    with subtests.test("idempotent when already absent"):
        result = await pacman_docker_protocol(PacmanRepository.absent, "rm-repo")
        assert result.changed is False


@pytest.mark.asyncio
async def test_pacman_repository_key(pacman_docker_protocol: Protocol, subtests) -> None:
    key_data = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake key\n-----END PGP PUBLIC KEY BLOCK-----\n"

    with subtests.test("installs key"):
        result = await pacman_docker_protocol(PacmanRepository.key, "test.asc", key_data)
        assert result.changed is True
        assert result.name == "test.asc"

    with subtests.test("idempotent with same key data"):
        result = await pacman_docker_protocol(PacmanRepository.key, "test.asc", key_data)
        assert result.changed is False

    with subtests.test("changed when key data differs"):
        result = await pacman_docker_protocol(PacmanRepository.key, "test.asc", b"different")
        assert result.changed is True
