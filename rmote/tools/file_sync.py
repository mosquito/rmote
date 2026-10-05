"""Incremental upload and download with atomic destination replacement."""

import asyncio
import atexit
import hashlib
import logging
import mimetypes
import os
import stat
import tempfile
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, ClassVar
from uuid import uuid4

from rmote.protocol import Tool


@dataclass(frozen=True)
class SyncResult:
    """Summary of a completed file synchronization.

    Attributes:
        changed: Whether the destination was created or replaced. False leaves
            its inode and timestamps unchanged; source metadata is not copied.
        size: Final file size in bytes.
        transferred: File-content bytes sent across the connection, before
            compression. Excludes signatures, RPC framing and transferred code.
        reused: Bytes taken from the existing destination instead of the
            connection. ``transferred + reused == size`` even when only
            truncating. They are written to the temporary file only when some
            other block differs.
    """

    changed: bool
    size: int
    transferred: int
    reused: int


@dataclass(frozen=True)
class Batch:
    """One message of a sender: new signatures, requested content, final hash.

    Attributes:
        signatures: ``(length, sha256_digest)`` of the next blocks of the
            source, in block order. The receiver answers one decision for each.
        blocks: Content of the blocks the receiver asked for, in the order it
            asked for them. Its size is bounded by ``Session.WINDOW``.
        digest: The hash over every block signature, present only in the last
            message. The receiver then verifies and installs the file.
    """

    signatures: list[tuple[int, bytes]]
    blocks: list[bytes]
    digest: bytes | None = None


@dataclass(frozen=True)
class Planned:
    """One planned block of a receiver, in block order.

    A receiver decides on a whole batch of signatures, but writes the blocks in
    order, so a block that matched after a block that did not must wait.
    """

    length: int
    signature: bytes
    matched: bool


def _version(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _open_regular(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class Session:
    """One sender or receiver in the block exchange used by :class:`FileSync`.

    Opens a regular file and keeps at most WINDOW bytes of content in memory:
    a sender holds the blocks it signed ahead, and gives one message of them at
    a time. A receiver opens a temporary file beside the destination at the
    first block that differs, and not at all while every block matches. Always
    call :meth:`close` in a ``finally`` block, including after successful
    completion. Calls on one session must be sequential. The ``lock``
    attribute keeps that order for a caller that steps the session from more
    than one thread; separate sessions never wait for each other.

    Args:
        path: Source or destination path in this process's filesystem.
        receiving: True to assemble a destination, False to read a source.
        block_size: Block size in bytes, from 1 through 16 MiB inclusive.
        size: Expected final size for a receiver; ignored by a sender, whose
            size is read from the open source file.

    Raises:
        ValueError: Invalid block size, negative size, or a special file.
        OSError: Opening the file fails, including missing source or parent,
            insufficient permissions, or a symlink at the supplied path.

    :meth:`step` advances the exchange and returns the message for the other
    session. A receiver returns :class:`SyncResult` after verifying the complete
    file and committing it. Application code normally uses :class:`FileSync`.
    """

    # Signatures that one message carries. A signature costs 32 bytes, so a
    # batch of 64 costs 2 KiB and saves 63 round trips on a link whose round
    # trip is long. The order of the signatures stays the order of the blocks,
    # so the hash over them does not depend on the size of a batch.
    SIGNATURE_BATCH: ClassVar[int] = 64

    # Content bytes that one message may carry. This bounds the memory of both
    # sides and decides how many blocks travel together: a 4 MiB block gives
    # two blocks per message, and a block of this size or more gives one.
    WINDOW: ClassVar[int] = 8 * 1024 * 1024

    def __init__(self, path: str, receiving: bool, block_size: int, size: int = 0) -> None:
        if not 0 < block_size <= 16 * 1024 * 1024:
            raise ValueError("block_size must be between 1 and 16777216")
        if size < 0:
            raise ValueError("size must be non-negative")
        self.path = Path(path).absolute()
        self.receiving = receiving
        self.block_size = block_size
        # One lock for one session, so a step of another session runs at the
        # same time. A step reads, hashes and writes up to WINDOW bytes.
        self.lock = threading.RLock()
        self.old: BinaryIO | None = None
        self.output: BinaryIO | None = None
        self.temp: Path | None = None
        self.initial: os.stat_result | None = None
        # Bytes signed by a sender, or committed to the output by a receiver.
        self.offset = 0
        # Sender: the blocks whose signatures are sent and not answered yet,
        # the blocks the receiver asked for and has not received yet, and the
        # content of the signed blocks that are kept for it, by offset.
        self.sent: list[tuple[int, int]] = []
        self.requests: deque[tuple[int, int]] = deque()
        self.cache: dict[int, bytes] = {}
        # True while every answered block matched. The destination then looks
        # like the source, and the signatures may run far ahead.
        self.matching = False
        # Receiver: the decided blocks that are not written yet, and the bytes
        # the decisions already cover.
        self.plan: deque[Planned] = deque()
        self.decided = 0
        # The final check is a hash over the block signatures, which both sides
        # compute anyway. Hashing the content a second time would double the
        # cost of a transfer that sends nothing.
        self.chain = hashlib.sha256()
        self.transferred = 0
        self.reused = 0
        try:
            try:
                self.old = _open_regular(self.path)
                self.initial = os.fstat(self.old.fileno())
            except FileNotFoundError:
                if not receiving:
                    raise
            self.size = size if receiving else self.initial.st_size if self.initial else 0
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        """Raise RuntimeError if the file's identity, size or timestamps changed.

        These checks detect ordinary concurrent writes; they do not lock the
        file or make a snapshot. Keep both files stable throughout the transfer.
        """
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            current = None
        if self.initial is None:
            if current is not None:
                raise RuntimeError("Destination appeared during transfer")
        elif current is None or _version(current) != _version(self.initial):
            raise RuntimeError("File changed during transfer")
        if self.old is not None and self.initial is not None:
            if _version(os.fstat(self.old.fileno())) != _version(self.initial):
                raise RuntimeError("Open file changed during transfer")

    def step(self, message: Any = None) -> Any:
        """Exchange a batch of signatures, the decisions on it, and content.

        Start the sender with None. It returns a :class:`Batch`: the signatures
        of its next blocks, the content of the blocks asked for earlier, and
        the final hash when nothing is left. Feed the batch to the receiver. It
        returns one decision per signature: True means its own block matched,
        and False asks for the content. Pass that list back to the sender.

        A batch with a digest ends the stream, and the receiver then returns
        SyncResult. Stop exchanging messages at that point and close both
        sessions.

        Raises:
            ValueError: Invalid signature, unexpected or corrupt content,
                unexpected decisions, incomplete transfer, or a final hash that
                does not match.
            RuntimeError: Either this file or its open descriptor changed.
            OSError: A read, write, fsync, metadata update or replacement fails.
        """
        self.check()
        return self.receive(message) if self.receiving else self.send(message)

    def send(self, decisions: Any) -> Batch:
        """Answer the decisions of the receiver with content and signatures.

        The content of the requested blocks goes first, so the receiver can
        write and the memory of both sides stays inside WINDOW. New signatures
        travel in the same message, which keeps one round trip per message.
        """
        if decisions is None:
            if self.sent:
                raise ValueError("Missing block decisions")
        else:
            if not isinstance(decisions, list) or len(decisions) != len(self.sent):
                raise ValueError("Unexpected block decisions")
            for block, matched in zip(self.sent, decisions, strict=True):
                if matched:
                    self.cache.pop(block[0], None)
                else:
                    self.requests.append(block)
            if decisions:
                self.matching = all(decisions)
            self.sent = []
        blocks = self.content()
        signatures = self.signatures()
        done = self.offset == self.size and not self.requests and not self.sent
        return Batch(signatures, blocks, self.chain.digest() if done else None)

    def read(self, offset: int, length: int) -> bytes:
        """Read one block of the source, or report that the source changed."""
        assert self.old is not None
        self.old.seek(offset)
        data = self.old.read(length)
        if len(data) != length:
            raise RuntimeError("Source size changed during transfer")
        return data

    def content(self) -> list[bytes]:
        """Read the blocks the receiver asked for, up to WINDOW bytes.

        A requested block is read from the source again, because the signatures
        run ahead of the content and keeping that many blocks would not bound
        the memory. The receiver checks every block against its signature, so a
        source that changes between the two reads is still found.
        """
        blocks: list[bytes] = []
        total = 0
        while self.requests:
            offset, length = self.requests[0]
            if blocks and total + length > self.WINDOW:
                break
            self.requests.popleft()
            data = self.cache.pop(offset, None)
            blocks.append(self.read(offset, length) if data is None else data)
            total += length
        return blocks

    def ahead(self) -> int:
        """Report how many blocks to sign in one message.

        While every answered block matched, the signatures run far ahead: one
        costs 32 bytes and no content is expected for it. As soon as a block
        differs, the batch shrinks to what the window holds, so every signed
        block stays in memory and its content needs no second read.
        """
        return self.SIGNATURE_BATCH if self.matching else max(1, self.WINDOW // self.block_size)

    def signatures(self) -> list[tuple[int, bytes]]:
        """Sign the next blocks of the source, up to the batch this mode allows."""
        batch: list[tuple[int, bytes]] = []
        limit = self.ahead()
        keep = not self.matching
        while len(batch) < limit and self.offset < self.size:
            length = min(self.block_size, self.size - self.offset)
            data = self.read(self.offset, length)
            if keep:
                self.cache[self.offset] = data
            signature = hashlib.sha256(data).digest()
            self.chain.update(signature)
            self.sent.append((self.offset, length))
            batch.append((length, signature))
            self.offset += length
        return batch

    def receive(self, message: Any) -> Any:
        """Write the content that arrived, then decide on the new signatures."""
        if not isinstance(message, Batch):
            raise ValueError("Invalid transfer message")
        for data in message.blocks:
            self.write(data)
            self.commit()
        decisions = [self.decide(length, signature) for length, signature in message.signatures]
        self.commit()
        if message.digest is not None:
            return self.finish(message.digest)
        return decisions

    def decide(self, length: int, signature: bytes) -> bool:
        """Compare one signature with the block of the destination at its place."""
        if length <= 0 or length != min(self.block_size, self.size - self.decided):
            raise ValueError("Invalid block signature")
        data = self.block(self.decided, length) if self.old is not None else b""
        matched = len(data) == length and hashlib.sha256(data).digest() == signature
        self.plan.append(Planned(length, signature, matched))
        self.decided += length
        if matched:
            self.reused += length
        return matched

    def block(self, offset: int, length: int) -> bytes:
        """Read one block of the destination, which can be shorter than asked."""
        assert self.old is not None
        self.old.seek(offset)
        return self.old.read(length)

    def write(self, data: bytes) -> None:
        """Write one requested block, after checking it against its signature."""
        if not self.plan or self.plan[0].matched:
            raise ValueError("Unexpected block content")
        planned = self.plan[0]
        if len(data) != planned.length or hashlib.sha256(data).digest() != planned.signature:
            raise ValueError("Block does not match its signature")
        self.plan.popleft()
        self.append(data, planned.signature)
        self.transferred += len(data)

    def commit(self) -> None:
        """Take the planned blocks the destination already holds, in order.

        A block that matched waits for the blocks before it, because the output
        is written in order. Only the blocks at the front of the plan can go.
        """
        while self.plan and self.plan[0].matched:
            planned = self.plan.popleft()
            self.keep(planned.length, planned.signature)

    def keep(self, length: int, signature: bytes) -> None:
        """Account for a block the destination already holds.

        Nothing is written while every block has matched: the destination
        already contains those bytes. Once a copy has started, the block is
        read from the destination and written into the copy.
        """
        if self.output is not None:
            data = self.block(self.offset, length)
            if len(data) != length:
                raise RuntimeError("Destination size changed during transfer")
            self.output.write(data)
        self.chain.update(signature)
        self.offset += length

    def append(self, data: bytes, signature: bytes | None = None) -> None:
        """Append already verified bytes to a receiver's temporary file.

        Starts the copy if it has not started, and updates the position and the
        chain of signatures. Use step for normal exchange: this helper does not
        check a block against its signature or count it as transferred.
        """
        if self.output is None:
            self.start()
        assert self.output is not None
        self.output.write(data)
        self.chain.update(hashlib.sha256(data).digest() if signature is None else signature)
        self.offset += len(data)

    def start(self) -> None:
        """Open the temporary file and copy the part that already matched.

        A receiver whose blocks all match writes nothing, so the copy begins at
        the first block that differs. The bytes before it are read from the
        destination, whose read position is restored afterwards.
        """
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.rmote-", dir=self.path.parent)
        self.temp = Path(name)
        self.output = os.fdopen(fd, "w+b")
        if not self.offset or self.old is None:
            return
        position = self.old.tell()
        self.old.seek(0)
        try:
            remaining = self.offset
            while remaining:
                chunk = self.old.read(min(self.block_size, remaining))
                if not chunk:
                    raise RuntimeError("Destination size changed during transfer")
                self.output.write(chunk)
                remaining -= len(chunk)
        finally:
            self.old.seek(position)

    def finish(self, digest: bytes) -> SyncResult:
        """Verify the hash over the block signatures and install the output.

        All bytes must have arrived and no requested block may be outstanding.
        An identical destination is left untouched, and nothing was written for
        it. Otherwise flush and fsync the temporary file, preserve existing
        mode/uid/gid, and use os.replace.
        New files have mode 0600. The containing directory is not fsynced, so
        this is atomic replacement, not a power-loss durability guarantee.

        Raises:
            ValueError: Incomplete transfer, wrong digest or a sender session.
            RuntimeError: The destination changed during transfer.
            OSError: Flushing, metadata preservation or replacement fails.

        Call close afterwards to release descriptors and any temporary file.
        """
        # A planned block that is not written yet keeps the position behind
        # the size, so one test covers both.
        if not self.receiving or self.offset != self.size:
            raise ValueError("Incomplete transfer")
        if self.chain.digest() != digest:
            raise ValueError("File digest mismatch")
        self.check()
        changed = self.initial is None or self.initial.st_size != self.size or self.transferred > 0
        if changed:
            if self.output is None:
                # Every block matched, but the destination is a different size,
                # so the part that matched becomes the new file.
                self.start()
            assert self.output is not None and self.temp is not None
            self.output.flush()
            # Preserve destination ownership and permissions. New files are private.
            if self.initial is not None:
                st = os.fstat(self.output.fileno())
                if (st.st_uid, st.st_gid) != (self.initial.st_uid, self.initial.st_gid):
                    os.fchown(self.output.fileno(), self.initial.st_uid, self.initial.st_gid)
                os.fchmod(self.output.fileno(), stat.S_IMODE(self.initial.st_mode))
            os.fsync(self.output.fileno())
            self.check()
            os.replace(self.temp, self.path)
        return SyncResult(changed, self.size, self.transferred, self.reused)

    def close(self) -> None:
        """Close file descriptors and remove any remaining temporary file.

        Does not install incomplete output or undo a completed replacement.
        Repeated calls are safe. Filesystem errors during cleanup propagate.
        """
        try:
            if self.old is not None:
                self.old.close()
            if self.output is not None:
                self.output.close()
        finally:
            if self.temp is not None:
                self.temp.unlink(missing_ok=True)


class FileSync(Tool):
    """Synchronize regular files through an open asynchronous Protocol.

    Call ``await FileSync.upload(protocol, local_path, remote_path)`` or
    ``await FileSync.download(protocol, remote_path, local_path)`` directly.
    These methods coordinate both machines locally; do not pass them as the
    tool argument to ``protocol(...)``. The target needs Python but no rmote
    installation. Paths are interpreted on their respective sides; parent
    directories must already exist and symlinks/special files are rejected.

    The sender hashes a block with SHA-256; the receiver compares its own block
    at that offset and requests content only on mismatch. The final check is a
    hash over those signatures, so neither side reads the content twice. Blocks
    default to 4 MiB.

    Signatures and content travel in batches, so one message serves many
    blocks and a long round trip is paid few times: 128 MiB of new content
    costs 18 calls with the default block, and 4 calls when the destination
    already holds it. Memory is bounded by ``Session.WINDOW``, not by the size
    of the file: a sender holds the content of one message and the blocks it
    signed ahead. Insertions can shift later block boundaries and cause large
    retransfers.

    Files smaller than one hashing block allow compression. For larger files,
    a MIME guess from the source name allows text, JSON and XML; encoded,
    binary and unknown formats use ``protocol.uncompressed`` when available.
    This is a tool policy, not content inspection or a transport guarantee.

    The receiver assembles a temporary file beside the destination, verifies
    the signatures, flushes and fsyncs, then atomically replaces it. Existing
    destination mode/uid/gid are preserved, new files use 0600. Source
    timestamps, ACLs and extended attributes are not copied. Other hard links
    still point to the old file. Identical files retain inode and timestamps,
    and nothing is written for them: the temporary file appears at the first
    block that differs, and the blocks before it are copied from the
    destination. Allow free space for the complete result, even if only one
    block differs.

    Keep both files stable: version checks detect ordinary concurrent changes
    but do not provide locking. Errors before replacement preserve the target.
    Cancellation waits for the current RPC before cleanup and can therefore
    wait on a stalled connection. An in-flight final replacement cannot be
    undone. Lost connections or abrupt process termination may leave a
    ``.<filename>.rmote-*`` temporary file; retry after an ambiguous final RPC
    failure to establish the resulting state. There is no resume or directory
    synchronization, and no guarantee of durability across power loss.

    Example using a real local subprocess (no SSH server required):

        >>> import asyncio
        >>> import sys
        >>> from pathlib import Path
        >>> from tempfile import TemporaryDirectory
        >>> from rmote.protocol import Protocol
        >>> async def example():
        ...     with TemporaryDirectory() as directory:
        ...         root = Path(directory)
        ...         source, target, copy = (root / n for n in ("source", "target", "copy"))
        ...         source.write_bytes(b"hello")
        ...         process = await asyncio.create_subprocess_exec(
        ...             sys.executable, "-qui", stdin=asyncio.subprocess.PIPE,
        ...             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        ...         )
        ...         try:
        ...             protocol = await Protocol.from_subprocess(process)
        ...             async with protocol:
        ...                 first = await FileSync.upload(protocol, source, target)
        ...                 again = await FileSync.upload(protocol, source, target)
        ...                 await FileSync.download(protocol, target, copy)
        ...                 return first.changed, again.changed, copy.read_bytes()
        ...         finally:
        ...             if process.returncode is None:
        ...                 process.terminate()
        ...             await process.wait()
        >>> asyncio.run(example())
        (True, False, b'hello')

    With SSH, enter ``async with await Protocol.from_ssh("user@host") as protocol``
    and use the same upload/download calls. The SSH protocol owns its process;
    the local subprocess example explicitly owns and reaps its child.
    """

    _sessions: ClassVar[dict[str, Session]] = {}
    # This lock protects the registry only: a lookup, an insertion or a
    # removal. File work runs under the lock of the session instead, so
    # transfers of different files do not wait for each other.
    _lock: ClassVar[threading.RLock] = threading.RLock()

    @staticmethod
    def _compress_file(path: str | Path, size: int, block_size: int) -> bool:
        """Tool policy: try small files and recognizable uncompressed text.

        The MIME guess uses the source name, not file content. Large unknown
        or binary formats take the raw path. Small means strictly less than
        one hashing block. This MIME policy does not belong to Protocol.
        """
        if size < block_size:
            return True
        mime, encoding = mimetypes.guess_type(str(path))
        if encoding or mime is None:
            return False
        return (
            mime.startswith("text/")
            or mime.endswith(("+xml", "+json"))
            or mime
            in {
                "application/json",
                "application/xml",
                "application/javascript",
                "application/sql",
            }
        )

    @staticmethod
    async def upload(
        protocol: Callable[..., Awaitable[Any]],
        local_path: str | Path,
        remote_path: str | Path,
        *,
        block_size: int = 4 * 1024 * 1024,
    ) -> SyncResult:
        """Synchronize local source content into a remote destination.

        Args:
            protocol: An open async Protocol, already entered with async with.
            local_path: Existing local source file; relative to local cwd.
            remote_path: Remote destination file; relative to the target cwd.
                Its parent must exist. A missing file is created with mode 0600.
            block_size: Bytes per block, 1 through 16 MiB; defaults to 4 MiB.

        Returns:
            SyncResult with changed status and transferred/reused byte counts.
            Repeating the call with identical content returns changed=False.

        Raises:
            ValueError: Invalid block size, special file, or failed integrity check.
            RuntimeError: A file changed while being synchronized.
            OSError: File access, temporary-file creation or installation fails.
            ConnectionError: The connection is closed. Transport failures may
                also propagate their original exception.
            asyncio.CancelledError: Cancelled after the in-flight RPC and cleanup.

        Uses the atomic replacement and metadata policy documented on FileSync.
        """
        return await FileSync.transfer(protocol, local_path, remote_path, True, block_size)

    @staticmethod
    async def download(
        protocol: Callable[..., Awaitable[Any]],
        remote_path: str | Path,
        local_path: str | Path,
        *,
        block_size: int = 4 * 1024 * 1024,
    ) -> SyncResult:
        """Synchronize remote source content into a local destination.

        Args:
            protocol: An open async Protocol, already entered with async with.
            remote_path: Existing remote source file; relative to target cwd.
            local_path: Local destination file; relative to local cwd. Its parent
                must exist. A missing file is created with mode 0600.
            block_size: Bytes per block, 1 through 16 MiB; defaults to 4 MiB.

        Returns:
            SyncResult with changed status and transferred/reused byte counts.

        Raises:
            ValueError: Invalid block size, special file, or failed integrity check.
            RuntimeError: A file changed while being synchronized.
            OSError: File access, temporary-file creation or installation fails.
            ConnectionError: Closed connection; other transport errors propagate.
            asyncio.CancelledError: Cancelled after the in-flight RPC and cleanup.

        Uses the same exchange and atomic replacement as upload, with the remote
        side as sender. Call directly: ``await FileSync.download(protocol, src, dst)``.
        """
        return await FileSync.transfer(protocol, local_path, remote_path, False, block_size)

    @staticmethod
    async def transfer(
        protocol: Callable[..., Awaitable[Any]],
        local_path: str | Path,
        remote_path: str | Path,
        uploading: bool,
        block_size: int,
    ) -> SyncResult:
        token = uuid4().hex
        cancelled = False
        finished = False

        async def run() -> SyncResult:
            nonlocal finished
            local = None
            try:
                if uploading:
                    local = await asyncio.to_thread(Session, str(local_path), False, block_size)
                    await protocol(FileSync._open, token, str(remote_path), True, block_size, local.size)
                else:
                    size = await protocol(FileSync._open, token, str(remote_path), False, block_size)
                    local = await asyncio.to_thread(Session, str(local_path), True, block_size, size)
                # The tool chooses; Protocol knows nothing about files or MIME.
                # Plain callable adapters retain their own compression policy.
                exchange = (
                    getattr(protocol, "uncompressed", protocol)
                    if not FileSync._compress_file(local_path if uploading else remote_path, local.size, block_size)
                    else protocol
                )
                message = None
                # Both sides run the same block exchange. Upload starts locally;
                # download starts remotely. Only one block is held on each side.
                while True:
                    for remote in (not uploading, uploading):
                        if cancelled:
                            raise asyncio.CancelledError
                        message = (
                            await exchange(FileSync._step, token, message)
                            if remote
                            else await asyncio.to_thread(local.step, message)
                        )
                        if isinstance(message, SyncResult):
                            # The remote session closed itself on its own final
                            # message, so the exchange needs no closing call.
                            finished = True
                            return message
            finally:
                try:
                    if local is not None:
                        await asyncio.to_thread(local.close)
                finally:
                    try:
                        if not finished:
                            await protocol(FileSync._close, token)
                    except Exception:
                        logging.getLogger(__name__).warning("Could not close remote file transfer", exc_info=True)

        # RPC cancellation does not stop a remote worker. Finish the current
        # exchange before cleaning up, without starting the next operation.
        worker = asyncio.create_task(run())
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise

    @classmethod
    def _open(cls, token: str, path: str, receiving: bool, block_size: int, size: int = 0) -> int:
        # The constructor opens a file, so it runs outside the registry lock.
        session = Session(path, receiving, block_size, size)
        try:
            with cls._lock:
                if token in cls._sessions:
                    raise ValueError("Session already exists")
                cls._sessions[token] = session
        except BaseException:
            session.close()
            raise
        return session.size

    @classmethod
    def _step(cls, token: str, message: Any = None) -> Any:
        with cls._lock:
            session = cls._sessions[token]
        # No thread waits for a session lock while it holds the registry lock,
        # so this order cannot deadlock.
        with session.lock:
            result = session.step(message)
            # A final message ends this side of the exchange: the result of a
            # receiver, or the batch of a sender that carries the final hash.
            # The session closes itself, so the caller needs no call for it.
            if isinstance(result, SyncResult) or (isinstance(result, Batch) and result.digest is not None):
                with cls._lock:
                    cls._sessions.pop(token, None)
                session.close()
            return result

    @classmethod
    def _close(cls, token: str) -> None:
        with cls._lock:
            session = cls._sessions.pop(token, None)
        if session is not None:
            # A step of this session can still run; wait for it to finish.
            with session.lock:
                session.close()


def _cleanup() -> None:
    for token in list(FileSync._sessions):
        FileSync._close(token)


atexit.register(_cleanup)
