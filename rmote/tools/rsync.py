"""Synchronize directory contents using FileSync for regular files."""

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
from typing import Any

from rmote.protocol import Tool
from rmote.tools.file_sync import FileSync


@dataclass(frozen=True)
class Entry:
    """One directory entry, inspected without following symbolic links."""

    kind: str
    mode: int
    target: str | None = None


@dataclass
class Result:
    """Completed directory synchronization, including content and mode changes.

    Attributes:
        changed: Whether any destination entry or permission changed.
        files: Number of regular files checked through FileSync.
        transferred: File content bytes sent; excludes RPC and signatures.
        reused: File content bytes reused from the destination.
        directories: Number of directories created, including the root.
        symlinks: Number of symbolic links created or replaced.
        deleted: Number of extra entries removed, including nested entries.
    """

    changed: bool = False
    files: int = 0
    transferred: int = 0
    reused: int = 0
    directories: int = 0
    symlinks: int = 0
    deleted: int = 0


class Rsync(Tool):
    """Synchronize the contents of a directory into another directory.

    Call upload/download directly with an open async Protocol. Both directions
    use the same traversal and compare every regular file through FileSync,
    even when size and mtime match. A trailing slash has no special meaning.
    Empty directories and symbolic links (including dangling links) are copied.
    Links are never traversed. Type conflicts and special source files raise
    ValueError. Extra destination entries are retained unless delete=True.

    Exclusions are case-sensitive globs over relative POSIX paths. A pattern
    without a slash matches an entry name at any depth (``*.pyc``, ``.git/``).
    A leading slash anchors to the root (``/cache/``); other patterns containing
    slashes are also root-relative (``build/**``). ``*``, ``?`` and ``[abc]``
    match within one component; a whole ``**`` component matches zero or more
    components. A trailing slash matches directories only, not symlinks.
    Patterns have no negation or ordering rules: any match excludes the path.
    For example, ``exclude=(".git/", "__pycache__/", "*.pyc", "build/**")``.
    Excluded directories are not traversed. Excluded destination entries and
    their necessary parents survive delete=True, including in destination-only
    subtrees. Their contents, modes and ownership are left untouched.

    Transfers are atomic per file, not per tree. Deletion starts only after all
    content transfers succeed. On failure, completed changes remain; retry to
    converge. Keep both trees stable and non-overlapping during synchronization;
    this is not a filesystem snapshot or protection against concurrent renames.
    Root paths and their parents must not be symbolic links. The destination's
    parent must exist; the destination root itself may be absent.

    preserve_mode copies permission bits, including executable bits, applying
    directory modes last. Owners, timestamps, ACLs, xattrs and hard-link identity
    are not copied. With preserve_mode=False, FileSync's metadata policy applies
    to files; new directories use 0700 and existing directory modes are kept.
    Existing destination directories must be writable for changed contents.
    owner/group optionally set destination ownership (names resolved there),
    including symlinks themselves. Without them, no ownership change is requested;
    new entries use the creating process and destination directory defaults.
    No external rsync executable is needed.

    Example with a real local subprocess:

        >>> import asyncio
        >>> import sys
        >>> from tempfile import TemporaryDirectory
        >>> from rmote.protocol import Protocol
        >>> async def example():
        ...     with TemporaryDirectory() as directory:
        ...         root = Path(directory).resolve()
        ...         source, target = root / "source", root / "target"
        ...         source.mkdir()
        ...         (source / "hello.txt").write_text("hello")
        ...         (source / ".git").mkdir()
        ...         (source / ".git" / "config").write_text("local only")
        ...         process = await asyncio.create_subprocess_exec(
        ...             sys.executable, "-qui", stdin=asyncio.subprocess.PIPE,
        ...             stdout=asyncio.subprocess.PIPE,
        ...         )
        ...         try:
        ...             async with await Protocol.from_subprocess(process) as protocol:
        ...                 first = await Rsync.upload(protocol, source, target, exclude=(".git/",))
        ...                 again = await Rsync.upload(protocol, source, target, exclude=(".git/",))
        ...                 assert not (target / ".git").exists()
        ...                 return first.changed, again.changed, (target / "hello.txt").read_text()
        ...         finally:
        ...             if process.returncode is None:
        ...                 process.terminate()
        ...             await process.wait()
        >>> asyncio.run(example())
        (True, False, 'hello')
    """

    @staticmethod
    async def upload(
        protocol: Callable[..., Awaitable[Any]],
        local_path: str | Path,
        remote_path: str | Path,
        *,
        delete: bool = False,
        concurrency: int = 4,
        preserve_mode: bool = True,
        block_size: int = 4 * 1024 * 1024,
        owner: str | None = None,
        group: str | None = None,
        exclude: Iterable[str] = (),
    ) -> Result:
        """Copy local directory contents into a remote directory.

        Args:
            protocol: Open async Protocol, already entered with async with.
            local_path: Source directory, relative to the local cwd.
            remote_path: Destination directory, relative to the remote cwd.
            delete: Remove destination-only entries after successful transfers.
            concurrency: Maximum simultaneous file transfers (positive integer).
            preserve_mode: Copy source file and directory permission bits.
            block_size: FileSync block size, between 1 byte and 16 MiB;
                defaults to 4 MiB. A file costs about two calls per block.
            owner: Explicit destination username; None keeps normal ownership.
            group: Explicit destination group name; None keeps normal grouping.
            exclude: Glob patterns for paths to leave untouched on both sides.
                See Rsync for matching and deletion rules.

        Returns:
            Aggregate change status and transfer counters.

        Raises:
            ValueError: Invalid options, unsupported source type, type conflict,
                or a symbolic link in a root path.
            OSError: A filesystem operation fails.
            RuntimeError: FileSync detects a file changing during transfer.
            ConnectionError: The remote connection fails.
            asyncio.CancelledError: Cancelled after active operations settle.
        """
        return await Rsync.transfer(
            protocol,
            local_path,
            remote_path,
            True,
            delete,
            concurrency,
            preserve_mode,
            block_size,
            owner,
            group,
            exclude,
        )

    @staticmethod
    async def download(
        protocol: Callable[..., Awaitable[Any]],
        remote_path: str | Path,
        local_path: str | Path,
        *,
        delete: bool = False,
        concurrency: int = 4,
        preserve_mode: bool = True,
        block_size: int = 4 * 1024 * 1024,
        owner: str | None = None,
        group: str | None = None,
        exclude: Iterable[str] = (),
    ) -> Result:
        """Copy remote directory contents into a local directory.

        Args:
            protocol: Open async Protocol, already entered with async with.
            remote_path: Source directory, relative to the remote cwd.
            local_path: Destination directory, relative to the local cwd.
            delete: Remove destination-only entries after successful transfers.
            concurrency: Maximum simultaneous file transfers (positive integer).
            preserve_mode: Copy source file and directory permission bits.
            block_size: FileSync block size, between 1 byte and 16 MiB;
                defaults to 4 MiB. A file costs about two calls per block.
            owner: Explicit destination username; None keeps normal ownership.
            group: Explicit destination group name; None keeps normal grouping.
            exclude: Glob patterns for paths to leave untouched on both sides.
                See Rsync for matching and deletion rules.

        Returns:
            Aggregate change status and transfer counters.

        Uses the same error, deletion and metadata policy as upload.
        """
        return await Rsync.transfer(
            protocol,
            local_path,
            remote_path,
            False,
            delete,
            concurrency,
            preserve_mode,
            block_size,
            owner,
            group,
            exclude,
        )

    @staticmethod
    def inspect(path: str) -> Entry | None:
        """Inspect a single entry without dereferencing it; None means absent."""
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return None
        kind = (
            "directory"
            if stat.S_ISDIR(mode)
            else "file"
            if stat.S_ISREG(mode)
            else "symlink"
            if stat.S_ISLNK(mode)
            else "special"
        )
        return Entry(kind, stat.S_IMODE(mode), os.readlink(path) if kind == "symlink" else None)

    @staticmethod
    def root(path: str, required: bool) -> str:
        """Validate and normalize a root, rejecting symlink path components."""
        value = Path(os.path.abspath(path))
        for parent in reversed((value, *value.parents)):
            entry = Rsync.inspect(str(parent))
            if entry is None and parent == value and not required:
                continue
            if entry is None:
                raise FileNotFoundError(str(parent))
            if entry.kind != "directory":
                raise ValueError(f"Not a real directory: {parent}")
        return str(value)

    @staticmethod
    def scan(path: str) -> dict[str, Entry]:
        """Read one directory, without following links or scanning descendants."""
        if (entry := Rsync.inspect(path)) is None or entry.kind != "directory":
            raise ValueError(f"Not a real directory: {path}")
        result = {}
        with os.scandir(path) as items:
            for item in items:
                entry = Rsync.inspect(item.path)
                if entry is None:
                    raise FileNotFoundError(item.path)
                result[item.name] = entry
        return result

    @staticmethod
    def mkdir(path: str) -> bool:
        """Create one missing directory with mode 0700."""
        entry = Rsync.inspect(path)
        if entry is not None:
            if entry.kind != "directory":
                raise ValueError(f"Not a directory: {path}")
            return False
        os.mkdir(path, 0o700)
        return True

    @staticmethod
    def link(path: str, target: str) -> bool:
        """Create or atomically replace a link; refuse other existing types."""
        entry = Rsync.inspect(path)
        if entry is not None:
            if entry.kind != "symlink":
                raise ValueError(f"Not a symbolic link: {path}")
            if entry.target == target:
                return False
        # Reserve a unique sibling path without following the destination link.
        import tempfile

        fd, temporary = tempfile.mkstemp(prefix=".rmote-link-", dir=str(Path(path).parent))
        os.close(fd)
        try:
            os.unlink(temporary)
            os.symlink(target, temporary)
            os.replace(temporary, path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        return True

    @staticmethod
    def chmod(path: str, mode: int) -> bool:
        """Set permission bits on a regular file or directory."""
        entry = Rsync.inspect(path)
        if entry is None or entry.kind not in {"file", "directory"}:
            raise ValueError(f"Not a regular file or directory: {path}")
        if entry.mode == mode:
            return False
        os.chmod(path, mode, follow_symlinks=False)
        return True

    @staticmethod
    def ownership(owner: str | None, group: str | None) -> tuple[int, int]:
        """Resolve explicitly requested names on the destination host."""
        import grp
        import pwd

        return (
            pwd.getpwnam(owner).pw_uid if owner is not None else -1,
            grp.getgrnam(group).gr_gid if group is not None else -1,
        )

    @staticmethod
    def chown(path: str, uid: int, gid: int) -> bool:
        """Apply explicit ownership to the entry itself, including symlinks."""
        current = os.lstat(path)
        if (uid == -1 or current.st_uid == uid) and (gid == -1 or current.st_gid == gid):
            return False
        os.chown(path, uid, gid, follow_symlinks=False)
        return True

    @staticmethod
    def patterns(exclude: Iterable[str]) -> tuple[str, ...]:
        """Validate exclusions before any filesystem operations."""
        if isinstance(exclude, (str, bytes)):
            raise TypeError("exclude must be an iterable of patterns, not a string")
        patterns = tuple(exclude)
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise TypeError("exclude patterns must be strings")
            parts = pattern.removeprefix("/").removesuffix("/").split("/")
            if any(part in {"", ".", ".."} for part in parts) or "\x00" in pattern:
                raise ValueError(f"Invalid exclude pattern: {pattern!r}")
        return patterns

    @staticmethod
    def excluded(relative: str, kind: str, patterns: tuple[str, ...]) -> bool:
        """Match a root-relative POSIX path without consulting the filesystem."""
        parts = relative.split("/")
        for pattern in patterns:
            if pattern.endswith("/") and kind != "directory":
                continue
            text = pattern.removesuffix("/")
            if "/" not in text:
                if fnmatchcase(parts[-1], text):
                    return True
                continue
            components = tuple(text.removeprefix("/").split("/"))

            @cache
            def match(i: int, j: int, components: tuple[str, ...] = components) -> bool:
                if j == len(components):
                    return i == len(parts)
                if components[j] == "**":
                    return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
                return i < len(parts) and fnmatchcase(parts[i], components[j]) and match(i + 1, j + 1)

            if match(0, 0):
                return True
        return False

    @staticmethod
    def remove(path: str, relative: str = "", patterns: tuple[str, ...] = ()) -> int:
        """Remove extra entries, retaining excluded descendants and their parents."""
        entry = Rsync.inspect(path)
        if entry is None or Rsync.excluded(relative, entry.kind, patterns):
            return 0
        count = 0
        if entry.kind == "directory":
            for name in Rsync.scan(path):
                count += Rsync.remove(str(Path(path) / name), f"{relative}/{name}", patterns)
            if Rsync.scan(path):
                return count
            os.rmdir(path)
        else:
            os.unlink(path)
        return count + 1

    @staticmethod
    async def transfer(
        protocol: Callable[..., Awaitable[Any]],
        local_path: str | Path,
        remote_path: str | Path,
        uploading: bool,
        delete: bool,
        concurrency: int,
        preserve_mode: bool,
        block_size: int,
        owner: str | None,
        group: str | None,
        exclude: Iterable[str],
    ) -> Result:
        """Shared local coordinator for both directions."""
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or not 0 < block_size <= 16 * 1024 * 1024:
            raise ValueError("block_size must be between 1 and 16777216")
        patterns = Rsync.patterns(exclude)
        result = Result()

        async def call(remote: bool, method: Callable[..., Any], *args: Any) -> Any:
            operation = asyncio.ensure_future(protocol(method, *args) if remote else asyncio.to_thread(method, *args))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # RPC and thread cancellation do not stop filesystem mutations.
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if not operation.cancelled():
                    operation.exception()
                raise

        local = await call(False, Rsync.root, str(local_path), uploading)
        remote = await call(True, Rsync.root, str(remote_path), not uploading)
        src, dst = (local, remote) if uploading else (remote, local)
        source_remote, target_remote = not uploading, uploading

        uid, gid = await call(target_remote, Rsync.ownership, owner, group)

        # Transfers that are running now, at most *concurrency* of them.
        active: set[asyncio.Task[None]] = set()

        async def reap(when: str = asyncio.FIRST_COMPLETED) -> None:
            """Wait for running transfers and raise the first failure of them."""
            if not active:
                return
            done, _ = await asyncio.wait(active, return_when=when)
            active.difference_update(done)
            for task in done:
                task.result()

        async def metadata(path: str, mode: int | None = None, present: int | None = None) -> None:
            if owner is not None or group is not None:
                result.changed |= await call(target_remote, Rsync.chown, path, uid, gid)
            # The scan reported the mode of both sides, and FileSync preserves
            # the mode of a destination it replaces, so an equal mode costs no
            # call at all.
            if preserve_mode and mode is not None and mode != present:
                result.changed |= await call(target_remote, Rsync.chmod, path, mode)

        async def file(relative: Path, entry: Entry, present: Entry | None) -> None:
            # FileSync opens both ends with O_NOFOLLOW and refuses anything but
            # a regular file, so a name replaced after the scan is rejected
            # there, without a call of our own to look at it again.
            fn = FileSync.upload if uploading else FileSync.download
            item = await fn(protocol, Path(src) / relative, Path(dst) / relative, block_size=block_size)
            result.files += 1
            result.transferred += item.transferred
            result.reused += item.reused
            result.changed |= item.changed
            await metadata(str(Path(dst) / relative), entry.mode, present.mode if present is not None else None)

        async def walk(relative: Path, finishing: bool = False) -> None:
            source = str(Path(src) / relative)
            target = str(Path(dst) / relative)
            entries = await call(source_remote, Rsync.scan, source)
            if not finishing:
                created = await call(target_remote, Rsync.mkdir, target)
                result.directories += created
                result.changed |= created
            existing = await call(target_remote, Rsync.scan, target)
            # Exclusion on either side protects the name even across type conflicts.
            protected = {
                name
                for items in (entries, existing)
                for name, entry in items.items()
                if Rsync.excluded((relative / name).as_posix(), entry.kind, patterns)
            }
            entries = {name: entry for name, entry in entries.items() if name not in protected}
            existing = {name: entry for name, entry in existing.items() if name not in protected}
            for name, entry in entries.items():
                if entry.kind == "special":
                    raise ValueError(f"Unsupported source entry: {Path(source) / name}")
                if name in existing and existing[name].kind != entry.kind:
                    raise ValueError(f"Type conflict: {Path(target) / name}")
            if not finishing:
                for name, entry in entries.items():
                    if entry.kind == "file":
                        # A sliding window: a transfer starts as soon as any
                        # running one finishes, so one slow file does not hold
                        # back the rest, and the window spans directories.
                        while len(active) >= concurrency:
                            await reap()
                        active.add(asyncio.create_task(file(relative / name, entry, existing.get(name))))
                    elif entry.kind == "symlink":
                        assert entry.target is not None
                        changed = await call(target_remote, Rsync.link, str(Path(target) / name), entry.target)
                        await metadata(str(Path(target) / name))
                        result.symlinks += changed
                        result.changed |= changed
            for name, entry in entries.items():
                if entry.kind == "directory":
                    await walk(relative / name, finishing)
            if finishing:
                if delete:
                    for name in existing.keys() - entries.keys():
                        count = await call(
                            target_remote,
                            Rsync.remove,
                            str(Path(target) / name),
                            (relative / name).as_posix(),
                            patterns,
                        )
                        result.deleted += count
                        result.changed |= bool(count)
                mode = (await call(source_remote, Rsync.inspect, source)).mode
                await metadata(target, mode)

        try:
            await walk(Path("."))
            # Every transfer must finish before the second pass sets the modes
            # of the directories and removes what the source does not have.
            await reap(asyncio.ALL_COMPLETED)
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
        await walk(Path("."), finishing=True)
        return result
