"""Exercise both roles over a real bootstrapped Python subprocess."""

import asyncio
import hashlib
from pathlib import Path
from typing import Literal

import pytest

from rmote.sync import Connection
from rmote.tools.file_sync import FileSync
from rmote.transfer import async_sync_file, sync_file


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize(
    ("original", "desired", "transferred"),
    [
        (None, b"", 0),
        (b"", b"", 0),
        (None, b"AAAABBBBCC", 10),
        (b"AAAABBBBCC", b"AAAABBBBCC", 0),
        (b"AAAABBBBCC", b"AAAAXXXXCC", 4),
        (b"AAAABBBBCC", b"AAAA", 0),
        (b"AAAABBBBCC", b"AAAAZ", 1),
        (b"AAAA", b"AAAABBBBCC", 6),
        (b"AAAA", b"", 0),
    ],
)
async def test_transfer(protocol, tmp_path, direction, original, desired, transferred):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(desired)
    if original is not None:
        target.write_bytes(original)
        target.chmod(0o640)
    before = target.stat() if target.exists() else None
    payloads = []

    async def remote(method, *args):
        if method == FileSync.write:
            payloads.append(args[1])
        result = await protocol(method, *args)
        if method == FileSync.data:
            payloads.append(result)
        return result

    result = await async_sync_file(remote, source, target, direction=direction, block_size=4)
    assert target.read_bytes() == desired
    assert result.size == len(desired)
    assert result.transferred == transferred
    assert sum(map(len, payloads)) == transferred
    assert all(len(block) <= 4 for block in payloads)
    assert result.reused == len(desired) - transferred
    assert result.changed == (original != desired)
    assert target.stat().st_mode & 0o777 == (0o640 if before else 0o600)
    if original == desired:
        assert before is not None
        assert target.stat().st_ino == before.st_ino
        assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(tmp_path.glob(".*.rmote-*"))
    assert not FileSync._sessions


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_sync_connection(tmp_path: Path, direction: Literal["upload", "download"]) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"test" * 20)
    with Connection.from_local() as connection:
        result = sync_file(connection, source, target, direction=direction, block_size=8)
        assert result.changed
        assert not sync_file(connection, source, target, direction=direction, block_size=8).changed
    assert source.read_bytes() == target.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("failure", ["corrupt", "source_changed", "target_changed", "rpc", "cancel"])
async def test_failure_preserves_target(protocol, tmp_path, direction, failure):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"AAAABBBB")
    target.write_bytes(b"original")
    touched = False
    task = None
    tokens = set()

    async def remote(method, *args):
        nonlocal touched
        tokens.add(args[0])
        if method == FileSync.write and failure == "corrupt":
            args = (args[0], b"xxxx")
        result = await protocol(method, *args)
        if method == FileSync.data and failure == "corrupt":
            return b"xxxx"
        if method == FileSync.match and not touched:
            touched = True
            if failure == "source_changed":
                source.write_bytes(b"changed!")
            elif failure == "target_changed":
                target.write_bytes(b"external")
            elif failure == "rpc":
                raise RuntimeError("Injected RPC failure")
            elif failure == "cancel":
                assert task is not None
                task.cancel()
        # Download's match happens locally; intercept its source signature instead.
        if direction == "download" and method == FileSync.signature and not touched:
            touched = True
            if failure == "source_changed":
                source.write_bytes(b"changed!")
            elif failure == "target_changed":
                target.write_bytes(b"external")
            elif failure == "rpc":
                raise RuntimeError("Injected RPC failure")
            elif failure == "cancel":
                assert task is not None
                task.cancel()
        return result

    task = asyncio.create_task(async_sync_file(remote, source, target, direction=direction, block_size=4))
    with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)):
        await task
    assert target.read_bytes() == (b"external" if failure == "target_changed" else b"original")
    assert not list(tmp_path.glob(".*.rmote-*"))
    assert not FileSync._sessions
    # Closed remote sessions cannot provide a source block.
    for token in tokens:
        with pytest.raises(KeyError):
            await protocol(FileSync.data, token)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["source", "target"])
async def test_rejects_symlinks(protocol, tmp_path, which):
    source, target, other = (tmp_path / name for name in ("source", "target", "other"))
    source.write_bytes(b"source")
    other.write_bytes(b"untouched")
    path = source if which == "source" else target
    path.unlink(missing_ok=True)
    path.symlink_to(other)
    with pytest.raises(OSError):
        await async_sync_file(protocol, source, target)
    assert other.read_bytes() == b"untouched"
    assert path.is_symlink()
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
async def test_missing_source(protocol, tmp_path):
    with pytest.raises(FileNotFoundError):
        await async_sync_file(protocol, tmp_path / "missing", tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_size", [0, -1, 16 * 1024 * 1024 + 1])
async def test_invalid_block_size(protocol, tmp_path, block_size):
    with pytest.raises(ValueError, match="block_size"):
        await async_sync_file(protocol, tmp_path / "missing", tmp_path / "target", block_size=block_size)


def test_final_digest_and_incomplete_transfer(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"old!")
    token = "digest-test"
    FileSync.begin(token, str(target), True, 4, 4)
    try:
        with pytest.raises(ValueError, match="Incomplete"):
            FileSync.finish(token, hashlib.sha256(b"new!").digest())
        assert not FileSync.match(token, 4, hashlib.sha256(b"new!").digest())
        FileSync.write(token, b"new!")
        with pytest.raises(ValueError, match="digest mismatch"):
            FileSync.finish(token, hashlib.sha256(b"wrong").digest())
        assert target.read_bytes() == b"old!"
    finally:
        FileSync.close(token)
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["upload", "download"])
async def test_target_unchanged_until_commit(protocol, tmp_path, direction):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"AAAAXXXX")
    target.write_bytes(b"AAAABBBB")
    original_inode = target.stat().st_ino
    seen = []

    async def remote(method, *args):
        if method not in (FileSync.close, FileSync.finish):
            assert target.read_bytes() == b"AAAABBBB"
            assert target.stat().st_ino == original_inode
        result = await protocol(method, *args)
        if method in (FileSync.match, FileSync.data):
            seen.append(method)
        return result

    result = await async_sync_file(remote, source, target, direction=direction, block_size=4)
    assert seen
    assert result.transferred == 4
    assert result.reused == 4
    assert target.read_bytes() == b"AAAAXXXX"
    assert target.stat().st_ino != original_inode


def test_replace_failure_preserves_original(tmp_path, monkeypatch):
    import os

    target = tmp_path / "target"
    target.write_bytes(b"old!")
    token = "replace-failure"
    digest = hashlib.sha256(b"new!").digest()
    FileSync.begin(token, str(target), True, 4, 4)
    try:
        assert not FileSync.match(token, 4, digest)
        FileSync.write(token, b"new!")

        def fail(*args):
            raise OSError("Injected replace failure")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="replace failure"):
            FileSync.finish(token, digest)
        assert target.read_bytes() == b"old!"
    finally:
        FileSync.close(token)
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
async def test_sync_helper_rejects_event_loop(tmp_path):
    with pytest.raises(RuntimeError, match="async_sync_file"):
        sync_file(lambda *args: None, tmp_path / "a", tmp_path / "b")
