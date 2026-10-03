"""Exercise both roles over a real bootstrapped Python subprocess."""

import asyncio
import hashlib

import pytest

from rmote.tools import FileSync
from rmote.tools.file_sync import Session


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
        if method == FileSync._step and isinstance(args[1], bytes):
            payloads.append(args[1])
        result = await protocol(method, *args)
        if method == FileSync._step and isinstance(result, bytes):
            payloads.append(result)
        return result

    result = await (FileSync.upload if direction == "upload" else FileSync.download)(
        remote, source, target, block_size=4
    )
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
        if method == FileSync._step and isinstance(args[1], bytes) and failure == "corrupt":
            args = (args[0], b"xxxx")
        result = await protocol(method, *args)
        if method == FileSync._step and isinstance(result, bytes) and failure == "corrupt":
            return b"xxxx"
        if method == FileSync._step and not touched:
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

    task = asyncio.create_task(
        (FileSync.upload if direction == "upload" else FileSync.download)(remote, source, target, block_size=4)
    )
    with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)):
        await task
    assert target.read_bytes() == (b"external" if failure == "target_changed" else b"original")
    assert not list(tmp_path.glob(".*.rmote-*"))
    assert not FileSync._sessions
    # Closed remote sessions cannot provide a source block.
    for token in tokens:
        with pytest.raises(KeyError):
            await protocol(FileSync._step, token)


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
        await FileSync.upload(protocol, source, target)
    assert other.read_bytes() == b"untouched"
    assert path.is_symlink()
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
async def test_missing_source(protocol, tmp_path):
    with pytest.raises(FileNotFoundError):
        await FileSync.upload(protocol, tmp_path / "missing", tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_size", [0, -1, 16 * 1024 * 1024 + 1])
async def test_invalid_block_size(protocol, tmp_path, block_size):
    with pytest.raises(ValueError, match="block_size"):
        await FileSync.upload(protocol, tmp_path / "missing", tmp_path / "target", block_size=block_size)


def test_final_digest_and_incomplete_transfer(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"old!")
    session = Session(str(target), True, 4, 4)
    try:
        with pytest.raises(ValueError, match="Incomplete"):
            session.step((0, hashlib.sha256(b"new!").digest()))
        assert not session.step((4, hashlib.sha256(b"new!").digest()))
        session.step(b"new!")
        with pytest.raises(ValueError, match="digest mismatch"):
            session.step((0, hashlib.sha256(b"wrong").digest()))
        assert target.read_bytes() == b"old!"
    finally:
        session.close()
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
        if method != FileSync._close:
            assert target.read_bytes() == b"AAAABBBB"
            assert target.stat().st_ino == original_inode
        result = await protocol(method, *args)
        if method == FileSync._step:
            seen.append(method)
        return result

    result = await (FileSync.upload if direction == "upload" else FileSync.download)(
        remote, source, target, block_size=4
    )
    assert seen
    assert result.transferred == 4
    assert result.reused == 4
    assert target.read_bytes() == b"AAAAXXXX"
    assert target.stat().st_ino != original_inode


def test_replace_failure_preserves_original(tmp_path, monkeypatch):
    import os

    target = tmp_path / "target"
    target.write_bytes(b"old!")
    digest = hashlib.sha256(b"new!").digest()
    session = Session(str(target), True, 4, 4)
    try:
        assert not session.step((4, digest))
        session.step(b"new!")

        def fail(*args):
            raise OSError("Injected replace failure")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="replace failure"):
            session.step((0, digest))
        assert target.read_bytes() == b"old!"
    finally:
        session.close()
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
async def test_round_trip_and_incremental_update(protocol, tmp_path):
    source, remote, copy = (tmp_path / name for name in ("source", "remote", "copy"))
    block_size = 1024 * 1024
    original = bytes(range(256)) * (2 * block_size // 256) + b"tail"
    source.write_bytes(original)
    first = await FileSync.upload(protocol, source, remote)
    assert first.transferred == len(original)
    assert (await FileSync.download(protocol, remote, copy)).transferred == len(original)
    assert copy.read_bytes() == original
    assert not (await FileSync.upload(protocol, source, remote)).changed
    assert not (await FileSync.download(protocol, remote, copy)).changed

    with source.open("r+b") as stream:
        stream.seek(block_size + 1)
        stream.write(b"!")
    for result in (
        await FileSync.upload(protocol, source, remote),
        await FileSync.download(protocol, remote, copy),
    ):
        assert result.transferred == block_size
        assert result.reused == len(original) - block_size
    assert copy.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_concurrent_transfers_share_protocol(protocol, tmp_path):
    a, b, remote_a, remote_b, copy = (tmp_path / name for name in ("a", "b", "remote-a", "remote-b", "copy"))
    a.write_bytes(b"abcd" * 8)
    b.write_bytes(b"xyz" * 9)
    await FileSync.upload(protocol, a, remote_a, block_size=4)
    uploaded, downloaded = await asyncio.gather(
        FileSync.upload(protocol, b, remote_b, block_size=3),
        FileSync.download(protocol, remote_a, copy, block_size=4),
    )
    assert uploaded.transferred == len(b.read_bytes())
    assert downloaded.transferred == len(a.read_bytes())
    assert remote_b.read_bytes() == b.read_bytes()
    assert copy.read_bytes() == a.read_bytes()
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["upload", "download"])
async def test_missing_destination_parent_cleans_up(protocol, tmp_path, direction):
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(FileNotFoundError):
        await (FileSync.upload if direction == "upload" else FileSync.download)(
            protocol, source, tmp_path / "missing" / "target"
        )
    assert source.read_bytes() == b"data"
    assert not (tmp_path / "missing").exists()
    assert not list(tmp_path.glob(".*.rmote-*"))
