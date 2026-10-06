"""Exercise both roles over a real bootstrapped Python subprocess."""

import asyncio
import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from rmote.tools import FileSync
from rmote.tools.file_sync import Batch, Session, SyncResult
from tests.support.synchronization import ObservedLock


@pytest.mark.asyncio
@pytest.mark.parametrize("uploading", [True, False])
@pytest.mark.parametrize("size", [1024, 65536, 65537, 2 * 1024 * 1024])
@pytest.mark.parametrize("filename", ["source.bin", "source.txt"])
async def test_file_sync_compression_policy_in_both_directions(
    protocol, tmp_path, monkeypatch, uploading, size, filename
):
    source, target = tmp_path / filename, tmp_path / "target"
    source.write_bytes(b"x" * size)
    assert protocol.FRAME_CODEC
    assert protocol.deflate is not None and protocol.inflate is not None
    transfer = FileSync.upload if uploading else FileSync.download
    # Warm tool transfer using a separate file before counting content.
    warm, warm_target = tmp_path / "warm", tmp_path / "warm-target"
    warm.write_bytes(b"warm")
    await transfer(protocol, warm, warm_target)
    wire = 0
    stream = protocol.writer if uploading else protocol.reader
    method = "write" if uploading else "feed_data"
    original = getattr(stream, method)

    def counted(data: bytes) -> None:
        nonlocal wire
        wire += len(data)
        original(data)

    with monkeypatch.context() as patch:
        patch.setattr(stream, method, counted)
        await transfer(protocol, source, target, block_size=65536)
    assert (wire < size) == (size < 65536 or filename.endswith(".txt"))
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    "name,compressed",
    [
        ("notes.txt", True),
        ("data.json", True),
        ("drawing.svg", True),
        ("photo.png", False),
        ("archive.tar.gz", False),
        ("unknown", False),
    ],
)
def test_file_compression_policy_uses_name_only_for_large_files(name, compressed):
    assert FileSync._compress_file(name, 1024, 65536)
    assert FileSync._compress_file(name, 1024 * 1024, 65536) is compressed


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
        if method == FileSync._step and isinstance(args[1], Batch):
            payloads.extend(args[1].blocks)
        result = await protocol(method, *args)
        if method == FileSync._step and isinstance(result, Batch):
            payloads.extend(result.blocks)
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

    def corrupted(batch: Batch) -> Batch:
        return replace(batch, blocks=[b"xxxx"] * len(batch.blocks))

    async def remote(method, *args):
        nonlocal touched
        tokens.add(args[0])
        if method == FileSync._step and isinstance(args[1], Batch) and args[1].blocks and failure == "corrupt":
            args = (args[0], corrupted(args[1]))
        result = await protocol(method, *args)
        if method == FileSync._step and isinstance(result, Batch) and result.blocks and failure == "corrupt":
            return corrupted(result)
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


def chained(*blocks: bytes) -> bytes:
    """The final check of a transfer: a hash over the signatures of its blocks."""
    return hashlib.sha256(b"".join(hashlib.sha256(block).digest() for block in blocks)).digest()


def test_final_digest_and_incomplete_transfer(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"old!")
    session = Session(str(target), True, 4, 4)
    try:
        with pytest.raises(ValueError, match="Incomplete"):
            session.step(Batch([], [], chained(b"new!")))
        assert session.step(Batch([(4, hashlib.sha256(b"new!").digest())], [])) == [False]
        assert session.step(Batch([], [b"new!"])) == []
        with pytest.raises(ValueError, match="digest mismatch"):
            session.step(Batch([], [], chained(b"wrong")))
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
    session = Session(str(target), True, 4, 4)
    try:
        assert session.step(Batch([(4, hashlib.sha256(b"new!").digest())], [])) == [False]
        session.step(Batch([], [b"new!"]))

        def fail(*args):
            raise OSError("Injected replace failure")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="replace failure"):
            session.step(Batch([], [], chained(b"new!")))
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
    # The block size is explicit: the file must span several blocks for the
    # incremental update below to mean anything.
    first = await FileSync.upload(protocol, source, remote, block_size=block_size)
    assert first.transferred == len(original)
    assert (await FileSync.download(protocol, remote, copy, block_size=block_size)).transferred == len(original)
    assert copy.read_bytes() == original
    assert not (await FileSync.upload(protocol, source, remote, block_size=block_size)).changed
    assert not (await FileSync.download(protocol, remote, copy, block_size=block_size)).changed

    with source.open("r+b") as stream:
        stream.seek(block_size + 1)
        stream.write(b"!")
    for result in (
        await FileSync.upload(protocol, source, remote, block_size=block_size),
        await FileSync.download(protocol, remote, copy, block_size=block_size),
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


@pytest.mark.asyncio
async def test_an_unchanged_destination_is_never_rewritten(protocol, tmp_path, monkeypatch):
    """A receiver whose blocks all match writes nothing beside the destination."""
    source, target = tmp_path / "source", tmp_path / "target"
    content = bytes(range(256)) * 64
    source.write_bytes(content)
    target.write_bytes(content)
    before = target.stat()
    created = []
    original = tempfile.mkstemp

    def counted(*args, **kwargs):
        created.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", counted)
    # A download receives locally, so the temporary file would appear here.
    result = await FileSync.download(protocol, source, target, block_size=1024)

    assert result == SyncResult(False, len(content), 0, len(content))
    assert created == []
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
async def test_the_copy_starts_at_the_first_block_that_differs(protocol, tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"AAAAXXXXCCCC")
    target.write_bytes(b"AAAABBBBCCCC")
    started = []
    original = Session.start

    def watched(self):
        started.append(self.offset)
        return original(self)

    monkeypatch.setattr(Session, "start", watched)
    result = await FileSync.download(protocol, source, target, block_size=4)

    # The first block matched, so the copy begins at the second and carries it.
    assert started == [4]
    assert target.read_bytes() == b"AAAAXXXXCCCC"
    assert result == SyncResult(True, 12, 4, 8)


@pytest.mark.asyncio
async def test_a_shorter_source_writes_the_part_that_matched(protocol, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"AAAA")
    target.write_bytes(b"AAAABBBB")

    result = await FileSync.download(protocol, source, target, block_size=4)

    assert result == SyncResult(True, 4, 0, 4)
    assert target.read_bytes() == b"AAAA"
    assert not list(tmp_path.glob(".*.rmote-*"))


@pytest.mark.asyncio
async def test_a_transfer_costs_few_calls(protocol, tmp_path):
    """Signatures and content travel in batches, so a transfer needs few calls."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) * 128)
    calls = []

    async def counted(method, *args):
        calls.append(method)
        return await protocol(method, *args)

    # Thirty-two blocks. One block per call cost 66 calls for this file.
    assert (await FileSync.upload(counted, source, target, block_size=1024)).changed
    assert len(calls) <= 6
    calls.clear()
    assert not (await FileSync.upload(counted, source, target, block_size=1024)).changed
    assert len(calls) <= 4


@pytest.mark.asyncio
async def test_the_window_bounds_the_content_of_one_message(protocol, tmp_path, monkeypatch):
    """A message carries at most WINDOW bytes of content, whatever the size."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) * 32)
    monkeypatch.setattr(Session, "WINDOW", 2048)
    sizes = []

    async def counted(method, *args):
        if method == FileSync._step and isinstance(args[1], Batch):
            sizes.append(sum(map(len, args[1].blocks)))
        return await protocol(method, *args)

    result = await FileSync.upload(counted, source, target, block_size=1024)

    assert result.transferred == 8192
    assert sum(sizes) == 8192
    assert max(sizes) == 2048


def test_unexpected_decisions_are_refused(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"AAAABBBB")
    session = Session(str(source), False, 4)
    try:
        assert len(session.step().signatures) == 2
        with pytest.raises(ValueError, match="Unexpected block decisions"):
            session.step([True])
        with pytest.raises(ValueError, match="Missing block decisions"):
            session.step(None)
    finally:
        session.close()


def test_unexpected_content_and_signatures_are_refused(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"old!")
    session = Session(str(target), True, 4, 4)
    try:
        with pytest.raises(ValueError, match="Invalid transfer message"):
            session.step((4, hashlib.sha256(b"new!").digest()))
        with pytest.raises(ValueError, match="Unexpected block content"):
            session.step(Batch([], [b"new!"]))
        with pytest.raises(ValueError, match="Invalid block signature"):
            session.step(Batch([(3, hashlib.sha256(b"new").digest())], []))
        assert target.read_bytes() == b"old!"
    finally:
        session.close()
    assert not list(tmp_path.glob(".*.rmote-*"))


def exchange(sender: Session, receiver: Session) -> SyncResult:
    """Run the whole block exchange between two sessions of this process."""
    message = None
    while True:
        reply = receiver.step(sender.step(message))
        if isinstance(reply, SyncResult):
            return reply
        message = reply


def test_a_requested_block_is_read_once(tmp_path, monkeypatch):
    """The sender keeps the blocks it signed, so content needs no second read.

    The window is small on purpose: the batch of signatures then spans several
    messages, as it does for a large file with the default window.
    """
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) * 32)
    monkeypatch.setattr(Session, "WINDOW", 2048)
    sender = Session(str(source), False, 1024)
    receiver = Session(str(target), True, 1024, sender.size)
    reads = []
    original = Session.read

    def counted(self, offset, length):
        reads.append(offset)
        return original(self, offset, length)

    monkeypatch.setattr(Session, "read", counted)
    try:
        result = exchange(sender, receiver)
    finally:
        sender.close()
        receiver.close()

    assert result == SyncResult(True, 8192, 8192, 0)
    assert target.read_bytes() == source.read_bytes()
    assert sorted(reads) == [index * 1024 for index in range(8)]


def test_a_sender_keeps_no_more_than_the_window(tmp_path, monkeypatch):
    """The blocks signed ahead stay inside the window, so memory is bounded."""
    source, target = tmp_path / "source", tmp_path / "target"
    content = bytes(range(256)) * 64
    source.write_bytes(content)
    # The destination matches at the start, so the sender first runs ahead
    # and then has to shrink its batch again.
    target.write_bytes(content[:2048] + bytes(len(content) - 2048))
    monkeypatch.setattr(Session, "WINDOW", 2048)
    sender = Session(str(source), False, 1024)
    receiver = Session(str(target), True, 1024, sender.size)
    held = []
    message = None
    try:
        while True:
            message = sender.step(message)
            held.append(sum(map(len, sender.cache.values())))
            assert sum(map(len, message.blocks)) <= sender.WINDOW
            reply = receiver.step(message)
            if isinstance(reply, SyncResult):
                break
            message = reply
    finally:
        sender.close()
        receiver.close()

    assert reply == SyncResult(True, len(content), len(content) - 2048, 2048)
    assert target.read_bytes() == content
    assert max(held) <= sender.WINDOW
    # Nothing is kept once every block is answered, matched or sent.
    assert not sender.cache


@pytest.mark.asyncio
async def test_a_destination_that_matches_needs_fewer_calls(protocol, tmp_path, monkeypatch):
    """While the blocks match, the signatures run far ahead of the window."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) * 128)
    monkeypatch.setattr(Session, "WINDOW", 2048)
    calls = []

    async def counted(method, *args):
        calls.append(method)
        return await protocol(method, *args)

    # Thirty-two blocks of content, two per message while they differ.
    assert (await FileSync.upload(counted, source, target, block_size=1024)).transferred == 32768
    assert 10 <= len(calls) <= 20
    calls.clear()
    assert not (await FileSync.upload(counted, source, target, block_size=1024)).changed
    # Every block matches, so one batch of signatures covers the whole file.
    assert len(calls) <= 5


def test_steps_of_different_sessions_run_at_the_same_time(tmp_path, monkeypatch):
    """A step of one session must not stop a step of another session."""
    tokens = ("first", "second")
    for token in tokens:
        source = tmp_path / token
        source.write_bytes(b"content")
        FileSync._open(token, str(source), False, 4)
    # A barrier that no step passes alone: a shared lock would break it.
    barrier = threading.Barrier(len(tokens), timeout=5)

    def waiting(self, message=None):
        barrier.wait()
        return Batch([], [])

    monkeypatch.setattr(Session, "step", waiting)
    try:
        with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
            steps = [pool.submit(FileSync._step, token) for token in tokens]
            for step in steps:
                assert step.result(timeout=5) == Batch([], [])
    finally:
        for token in tokens:
            FileSync._close(token)
    assert not FileSync._sessions


def test_steps_of_one_session_do_not_overlap(tmp_path, monkeypatch):
    """The lock of a session keeps the order of its own steps."""
    source = tmp_path / "source"
    source.write_bytes(b"content")
    FileSync._open("token", str(source), False, 4)
    active = 0
    maximum = 0
    entered = threading.Event()
    release = threading.Event()
    state = threading.Lock()
    session = FileSync._sessions["token"]
    lock = ObservedLock(session.lock)
    monkeypatch.setattr(session, "lock", lock)

    def counting(self, message=None):
        nonlocal active, maximum
        with state:
            active += 1
            maximum = max(maximum, active)
        entered.set()
        assert release.wait(10)
        with state:
            active -= 1
        return Batch([], [])

    monkeypatch.setattr(Session, "step", counting)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            steps = [pool.submit(FileSync._step, "token") for _ in range(4)]
            try:
                lock.wait(4)
                assert entered.wait(5)
                assert active == 1
            finally:
                release.set()
            for step in steps:
                step.result(timeout=5)
    finally:
        FileSync._close("token")
    assert maximum == 1
    assert not FileSync._sessions


def test_a_taken_token_is_refused_and_keeps_the_first_session(tmp_path):
    """A duplicate token must not replace or close the open session."""
    source = tmp_path / "source"
    source.write_bytes(b"content")
    assert FileSync._open("token", str(source), False, 4) == 7
    try:
        with pytest.raises(ValueError, match="Session already exists"):
            FileSync._open("token", str(source), False, 4)
        assert list(FileSync._sessions) == ["token"]
        assert FileSync._step("token").signatures
    finally:
        FileSync._close("token")
    assert not FileSync._sessions
    assert not list(tmp_path.glob(".*.rmote-*"))
