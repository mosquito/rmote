"""Stateful endpoints for blockwise file transfers (normally used via rmote.transfer)."""

import atexit
import hashlib
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar

from rmote.protocol import Tool


@dataclass(frozen=True)
class SyncResult:
    changed: bool
    size: int
    transferred: int
    reused: int


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


class _Session:
    def __init__(self, path: str, receiving: bool, block_size: int, size: int = 0) -> None:
        if not 0 < block_size <= 16 * 1024 * 1024:
            raise ValueError("block_size must be between 1 and 16777216")
        if size < 0:
            raise ValueError("size must be non-negative")
        self.path = Path(path).absolute()
        self.receiving = receiving
        self.block_size = block_size
        self.old: BinaryIO | None = None
        self.output: BinaryIO | None = None
        self.temp: Path | None = None
        self.initial: os.stat_result | None = None
        self.offset = 0
        self.pending: bytes | None = None
        self.expected: tuple[int, bytes] | None = None
        self.digest = hashlib.sha256()
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
            if receiving:
                fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.rmote-", dir=self.path.parent)
                self.temp = Path(name)
                self.output = os.fdopen(fd, "w+b")
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
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

    def signature(self) -> tuple[int, bytes]:
        if self.receiving:
            raise ValueError("Not a sender")
        self.check()
        assert self.old is not None
        data = self.old.read(min(self.block_size, self.size - self.offset))
        if len(data) != min(self.block_size, self.size - self.offset):
            raise RuntimeError("Source size changed during transfer")
        self.pending = data
        self.offset += len(data)
        self.digest.update(data)
        return len(data), hashlib.sha256(data).digest()

    def match(self, length: int, digest: bytes) -> bool:
        if not self.receiving or self.expected is not None:
            raise ValueError("Receiver is not ready for a signature")
        if length != min(self.block_size, self.size - self.offset) or length <= 0:
            raise ValueError("Invalid block length")
        self.check()
        data = self.old.read(length) if self.old is not None else b""
        if len(data) == length and hashlib.sha256(data).digest() == digest:
            self.append(data)
            self.reused += length
            return True
        self.expected = length, digest
        return False

    def append(self, data: bytes) -> None:
        assert self.output is not None
        self.output.write(data)
        self.digest.update(data)
        self.offset += len(data)

    def write(self, data: bytes) -> None:
        if self.expected != (len(data), hashlib.sha256(data).digest()):
            raise ValueError("Block does not match its signature")
        self.append(data)
        self.transferred += len(data)
        self.expected = None

    def finish(self, digest: bytes) -> SyncResult:
        if not self.receiving or self.expected is not None or self.offset != self.size:
            raise ValueError("Incomplete transfer")
        if self.digest.digest() != digest:
            raise ValueError("File digest mismatch")
        self.check()
        changed = self.initial is None or self.initial.st_size != self.size or self.transferred > 0
        if changed:
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
        try:
            if self.old is not None:
                self.old.close()
            if self.output is not None:
                self.output.close()
        finally:
            if self.temp is not None:
                self.temp.unlink(missing_ok=True)


class FileSync(Tool):
    """Low-level transfer sessions. Each session must be closed, including on failure.

    IDs are chosen by the controller so failed begin responses can be cleaned up.
    Calls for one session are serialized; independent sessions may run concurrently.
    """

    _sessions: ClassVar[dict[str, _Session]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()

    @classmethod
    def begin(cls, token: str, path: str, receiving: bool, block_size: int, size: int = 0) -> int:
        with cls._lock:
            if token in cls._sessions:
                raise ValueError("Session already exists")
            session = _Session(path, receiving, block_size, size)
            cls._sessions[token] = session
            return session.size

    @classmethod
    def signature(cls, token: str) -> tuple[int, bytes]:
        with cls._lock:
            return cls._sessions[token].signature()

    @classmethod
    def match(cls, token: str, length: int, digest: bytes) -> bool:
        with cls._lock:
            return cls._sessions[token].match(length, digest)

    @classmethod
    def data(cls, token: str) -> bytes:
        with cls._lock:
            session = cls._sessions[token]
            if session.receiving or session.pending is None:
                raise ValueError("No pending source block")
            return session.pending

    @classmethod
    def write(cls, token: str, data: bytes) -> None:
        with cls._lock:
            cls._sessions[token].write(data)

    @classmethod
    def digest(cls, token: str) -> bytes:
        with cls._lock:
            session = cls._sessions[token]
            if session.receiving or session.offset != session.size:
                raise ValueError("Source not fully read")
            session.check()
            return session.digest.digest()

    @classmethod
    def finish(cls, token: str, digest: bytes) -> SyncResult:
        with cls._lock:
            return cls._sessions[token].finish(digest)

    @classmethod
    def close(cls, token: str) -> None:
        with cls._lock:
            session = cls._sessions.pop(token, None)
            if session is not None:
                session.close()


def _cleanup() -> None:
    for token in list(FileSync._sessions):
        FileSync.close(token)


atexit.register(_cleanup)
