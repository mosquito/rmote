"""FileSync across a real container boundary, without shared filesystem mounts."""

import hashlib
import logging
import random
from pathlib import Path

import pytest

from rmote.protocol import Protocol
from rmote.tools import Exec, FileSync

MIB = 1024 * 1024


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("size_mib", [1, 2, 5, 10, 64], ids=lambda size: f"{size}MiB")
async def test_docker_file_sizes(
    docker_protocol: Protocol, tmp_path: Path, caplog: pytest.LogCaptureFixture, size_mib: int
) -> None:
    # Protocol DEBUG logs include payloads; avoid retaining large binary reprs.
    caplog.set_level(logging.WARNING)
    source, downloaded = tmp_path / "source.bin", tmp_path / "downloaded.bin"
    remote_path = f"/tmp/rmote-transfer-{size_mib}.bin"
    size = size_mib * MIB
    random_bytes = random.Random(42)
    # Distinct, reproducible, incompressible blocks expose ordering/copy errors.
    # Build and verify files incrementally instead of holding 64 MiB in memory.
    with source.open("wb") as stream:
        for _ in range(size_mib):
            stream.write(random_bytes.randbytes(MIB))
    expected = file_digest(source)

    upload = await FileSync.upload(docker_protocol, source, remote_path)
    assert upload.changed
    assert (upload.size, upload.transferred, upload.reused) == (size, size, 0)
    checksum = await docker_protocol(Exec.command, "sha256sum", remote_path, capture_output=True)
    assert checksum.stdout is not None
    assert checksum.stdout.decode().split()[0] == expected

    download = await FileSync.download(docker_protocol, remote_path, downloaded)
    assert download.changed
    assert (download.size, download.transferred, download.reused) == (size, size, 0)
    assert downloaded.stat().st_size == size
    assert file_digest(downloaded) == expected

    for repeated in (
        await FileSync.upload(docker_protocol, source, remote_path),
        await FileSync.download(docker_protocol, remote_path, downloaded),
    ):
        assert not repeated.changed
        assert (repeated.size, repeated.transferred, repeated.reused) == (size, 0, size)

    # Change one byte in the middle; exactly one 1 MiB block must cross each way.
    with source.open("r+b") as stream:
        stream.seek(size // 2)
        original = stream.read(1)
        stream.seek(size // 2)
        stream.write(bytes([original[0] ^ 0xFF]))
    updated = file_digest(source)
    assert updated != expected
    for incremental in (
        await FileSync.upload(docker_protocol, source, remote_path),
        await FileSync.download(docker_protocol, remote_path, downloaded),
    ):
        assert incremental.changed
        assert (incremental.size, incremental.transferred, incremental.reused) == (size, MIB, size - MIB)
    assert file_digest(downloaded) == updated
    checksum = await docker_protocol(Exec.command, "sha256sum", remote_path, capture_output=True)
    assert checksum.stdout is not None
    assert checksum.stdout.decode().split()[0] == updated
    assert not list(tmp_path.glob(".*.rmote-*"))
