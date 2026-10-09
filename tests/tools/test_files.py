"""Descriptor semantics across a real bootstrapped rmote connection."""

import errno
import os
from uuid import uuid4

import pytest

from rmote.tools.files import Files, OpenFlags


@pytest.mark.asyncio
async def test_handles_survive_rename_and_unlink_and_are_session_owned(protocol, tmp_path):
    first, second = await protocol(Files.start), await protocol(Files.start)
    assert first != second
    path, renamed = tmp_path / "file", tmp_path / "renamed"
    try:
        handle = await protocol(
            Files.open, first, str(path), OpenFlags.READ | OpenFlags.WRITE | OpenFlags.CREAT | OpenFlags.EXCL
        )
        await protocol(Files.write, first, handle, 4, b"tail")
        await protocol(Files.write, first, handle, 0, b"head")
        with pytest.raises(OSError) as error:
            await protocol(Files.read, second, handle, 0, 8)
        assert error.value.errno == errno.EBADF
        other = await protocol(Files.open, second, str(tmp_path / "other"), 1 | 2 | 8)
        await protocol(Files.write, second, other, 0, b"other")
        with pytest.raises(OSError) as error:
            await protocol(Files.write, second, handle, 0, b"wrong session")
        assert error.value.errno == errno.EBADF
        await protocol(Files.release, first)
        assert await protocol(Files.read, second, other, 0, 100) == b"other"
        first = await protocol(Files.start)
        with pytest.raises(OSError):
            await protocol(Files.write, first, handle, 0, b"stale handle")
        handle = await protocol(Files.open, first, str(path), 1 | 2)
        await protocol(Files.rename, first, str(path), str(renamed), True)
        await protocol(Files.remove, first, str(renamed))
        assert await protocol(Files.read, first, handle, 0, 100) == b"headtail"
        await protocol(Files.fsetstat, first, handle, {"size": 4})
        assert (await protocol(Files.fstat, first, handle)).st_size == 4
        await protocol(Files.close, first, handle)
        with pytest.raises(OSError):
            await protocol(Files.read, first, handle, 0, 1)
    finally:
        await protocol(Files.release, first)
        await protocol(Files.release, second)


@pytest.mark.asyncio
async def test_open_append_exclusive_and_directory_batches(protocol, tmp_path):
    session_id = uuid4().hex
    await protocol(Files.start, session_id)
    try:
        path = tmp_path / "file"
        path.write_bytes(b"first")
        handle = await protocol(Files.open, session_id, str(path), 2 | 4)
        await protocol(Files.write, session_id, handle, 0, b"second")
        await protocol(Files.close, session_id, handle)
        assert path.read_bytes() == b"firstsecond"
        with pytest.raises(FileExistsError):
            await protocol(Files.open, session_id, str(path), 2 | 8 | 32)
        for i in range(130):
            (tmp_path / str(i)).touch()
        directory = await protocol(Files.opendir, session_id, str(tmp_path))
        names: list[str] = []
        while batch := await protocol(Files.readdir, session_id, directory):
            assert len(batch) <= 64
            names.extend(name for name, _ in batch)
        assert len(names) == len(set(names)) == 131
    finally:
        await protocol(Files.release, session_id)


def test_release_closes_files_and_directory_handles(tmp_path):
    session_id = uuid4().hex
    Files.start(session_id)
    handle = Files.open(session_id, str(tmp_path / "file"), 2 | 8)
    directory = Files.opendir(session_id, str(tmp_path))
    descriptors = [Files._sessions[session_id].handles[key].fd for key in (handle, directory)]
    Files.release(session_id)
    Files.release(session_id)
    assert session_id not in Files._sessions
    for fd in descriptors:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


def test_partial_writes_and_handle_limits(tmp_path, monkeypatch):
    session_id = uuid4().hex
    Files.start(session_id)
    try:
        path = tmp_path / "file"
        handle = Files.open(session_id, str(path), 1 | 2 | 8)
        original = os.pwrite
        monkeypatch.setattr(os, "pwrite", lambda fd, data, offset: original(fd, data[:2], offset))
        Files.write(session_id, handle, 0, b"abcdefg")
        assert path.read_bytes() == b"abcdefg"
        monkeypatch.setattr(Files, "MAX_HANDLES", 1)
        with pytest.raises(OSError) as error:
            Files.open(session_id, str(path), 1)
        assert error.value.errno == errno.EMFILE
        with pytest.raises(OSError):
            Files.opendir(session_id, str(tmp_path))
    finally:
        Files.release(session_id)


def test_fifo_open_does_not_block(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    session_id = uuid4().hex
    Files.start(session_id)
    try:
        with pytest.raises(OSError):
            Files.open(session_id, str(path), 1)
        assert not Files._sessions[session_id].handles
    finally:
        Files.release(session_id)


@pytest.mark.parametrize("flags", [0x40 | 2 | 8, -1])
def test_unknown_open_flags_do_not_create_files(tmp_path, flags):
    session_id = Files.start()
    path = tmp_path / "file"
    try:
        with pytest.raises(ValueError, match="Invalid file open flags"):
            Files.open(session_id, str(path), flags)
        assert not path.exists()
        assert not Files._sessions[session_id].handles
    finally:
        Files.release(session_id)
