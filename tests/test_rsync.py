"""Directory synchronization through a real isolated Python subprocess."""

import asyncio
import grp
import os
import pwd
import stat

import pytest

from rmote.tools import FileSync, Rsync


@pytest.fixture(params=["upload", "download"])
def sync(request, protocol):
    async def run(source, target, **kwargs):
        return await getattr(Rsync, request.param)(protocol, source, target, **kwargs)

    return run


async def test_tree_roundtrip_and_incremental(sync, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "nested" / "empty").mkdir(parents=True)
    (source / "nested" / "script").write_bytes(b"AAAABBBBCCCC")
    (source / "nested" / "script").chmod(0o751)
    (source / "empty").touch()
    (source / "link").symlink_to("nested/script")
    (source / "dangling").symlink_to("missing")
    first = await sync(source, target, block_size=4)
    assert first.changed and first.files == 2 and first.directories == 3
    assert first.transferred == 12 and first.symlinks == 2
    assert (target / "nested" / "empty").is_dir()
    assert (target / "nested" / "script").read_bytes() == b"AAAABBBBCCCC"
    assert stat.S_IMODE((target / "nested" / "script").stat().st_mode) == 0o751
    assert os.readlink(target / "link") == "nested/script"
    assert os.readlink(target / "dangling") == "missing"
    again = await sync(source, target, block_size=4)
    assert not again.changed and again.transferred == 0 and again.reused == 12
    old_time = (source / "nested" / "script").stat()
    (source / "nested" / "script").write_bytes(b"AAAAXXXXCCCC")
    os.utime(source / "nested" / "script", ns=(old_time.st_atime_ns, old_time.st_mtime_ns))
    updated = await sync(source, target, block_size=4)
    assert updated.changed and updated.transferred == 4 and updated.reused == 8
    (source / "link").unlink()
    (source / "link").symlink_to("empty")
    assert (await sync(source, target)).symlinks == 1
    assert os.readlink(target / "link") == "empty"


async def test_delete_and_no_follow(sync, tmp_path):
    source, target, outside = (tmp_path / n for n in ("src", "dst", "outside"))
    source.mkdir()
    target.mkdir()
    outside.mkdir()
    (outside / "keep").write_text("outside")
    (target / "extra").mkdir()
    (target / "extra" / "data").write_text("extra")
    (target / "outside").symlink_to(outside, target_is_directory=True)
    assert not (await sync(source, target)).changed
    result = await sync(source, target, delete=True)
    assert result.deleted == 3 and result.changed
    assert list(target.iterdir()) == []
    assert (outside / "keep").read_text() == "outside"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
async def test_conflicts_leave_extra_entries(sync, tmp_path, kind):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "item").write_text("source")
    if kind == "directory":
        (target / "item").mkdir()
    elif kind == "symlink":
        (target / "item").symlink_to(source / "item")
    else:
        (source / "item").unlink()
        (source / "item").mkdir()
        (target / "item").write_text("destination")
    (target / "extra").write_text("keep")
    with pytest.raises(ValueError, match="Type conflict"):
        await sync(source, target, delete=True)
    assert (target / "extra").read_text() == "keep"


async def test_special_source_and_missing_root(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    with pytest.raises(FileNotFoundError):
        await sync(source, target)
    assert not target.exists()
    source.mkdir()
    os.mkfifo(source / "pipe")
    with pytest.raises(ValueError, match="Unsupported source"):
        await sync(source, target)


async def test_symlink_root_and_parent_rejected(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    link = tmp_path / "link"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        await sync(link, target)
    with pytest.raises(ValueError, match="real directory"):
        await sync(source, link / "child")
    assert not (source / "child").exists()


async def test_modes_and_explicit_ownership(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir(mode=0o750)
    (source / "file").write_text("data")
    (source / "file").chmod(0o751)
    (source / "link").symlink_to("file")
    await sync(source, target, preserve_mode=False)
    assert stat.S_IMODE((target / "file").stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    uid, gid = target.stat().st_uid, (target / "file").stat().st_gid
    assert (target / "file").stat().st_uid == os.geteuid()
    result = await sync(source, target, owner=pwd.getpwuid(uid).pw_name, group=grp.getgrgid(gid).gr_name)
    assert result.changed
    assert stat.S_IMODE((target / "file").stat().st_mode) == 0o751
    assert (target / "link").lstat().st_uid == uid
    assert not (await sync(source, target)).changed


async def test_invalid_options_before_mutation(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    for options in ({"concurrency": 0}, {"concurrency": True}, {"block_size": 0}, {"block_size": 2**25}):
        with pytest.raises(ValueError):
            await sync(source, target, **options)
        assert not target.exists()
    with pytest.raises(KeyError):
        await sync(source, target, owner="rmote-user-that-does-not-exist-123")
    assert not target.exists()


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_settles_transfers_and_prevents_delete(protocol, tmp_path, direction, cancel):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    for i in range(5):
        (source / str(i)).write_bytes(b"abcd" * 1024)
    (target / "extra").write_text("keep")
    active = set()
    peak = 0
    task = None
    triggered = False

    async def remote(method, *args):
        nonlocal peak, triggered
        if method == FileSync._open:
            active.add(args[0])
            peak = max(peak, len(active))
        result = await protocol(method, *args)
        if method == FileSync._close:
            active.discard(args[0])
        if method == FileSync._step and not triggered:
            triggered = True
            if cancel:
                assert task is not None
                task.cancel()
            else:
                raise RuntimeError("injected")
        return result

    task = asyncio.create_task(
        getattr(Rsync, direction)(remote, source, target, delete=True, concurrency=2, block_size=4)
    )
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task
    assert triggered and 1 <= peak <= 2 and not active
    assert (target / "extra").read_text() == "keep"
    assert not list(target.glob(".*.rmote-*"))
    assert not FileSync._sessions


# All tests above use asynchronous calls, including both fixture directions.
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("direction", ["upload", "download"])
async def test_bounded_parallel_transfers(protocol, tmp_path, direction):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    for i in range(7):
        (source / str(i)).write_bytes(b"abcd" * 10)
    active = set()
    two_started = asyncio.Event()
    peak = 0

    async def remote(method, *args):
        nonlocal peak
        result = await protocol(method, *args)
        if method == FileSync._open:
            active.add(args[0])
            peak = max(peak, len(active))
            if len(active) == 2:
                two_started.set()
            await asyncio.wait_for(two_started.wait(), 5)
        elif method == FileSync._close:
            active.remove(args[0])
        return result

    result = await getattr(Rsync, direction)(remote, source, target, concurrency=2, block_size=4)
    assert result.files == 7 and result.transferred == 280
    assert peak == 2 and not active


@pytest.mark.parametrize(
    ("patterns", "omitted"),
    [
        (("*.pyc",), {"root.pyc", "nested/cache.pyc"}),
        (("/root.*",), {"root.pyc", "root.txt"}),
        ((".git/",), {".git/config", "nested/.git/config"}),
        (("/.git/",), {".git/config"}),
        (("build/**",), {"build/output", "build/nested/item"}),
        (("**/build/**",), {"build/output", "build/nested/item", "pkg/build/output"}),
        (("nested/*.txt",), {"nested/top.txt"}),
        (("**/log?.txt",), {"nested/deep/log1.txt", "nested/deep/log2.txt"}),
        (("nested/**/log[12].txt",), {"nested/deep/log1.txt", "nested/deep/log2.txt"}),
        (("**/top.txt",), {"nested/top.txt"}),
        ((".*",), {".git/config", "nested/.git/config", ".hidden"}),
        ((), set()),
    ],
)
async def test_exclude_globs(sync, tmp_path, patterns, omitted):
    source, target = tmp_path / "src", tmp_path / "dst"
    names = {
        "root.pyc",
        "upper.PYC",
        "root.txt",
        "nested/cache.pyc",
        "nested/deep/log1.txt",
        "nested/deep/log2.txt",
        "nested/top.txt",
        ".git/config",
        "nested/.git/config",
        "build/output",
        "build/nested/item",
        "pkg/build/output",
        ".hidden",
    }
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    result = await sync(source, target, exclude=iter(patterns))
    copied = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert copied == names - omitted
    assert result.files == len(copied)
    assert result.transferred == sum(len(name) for name in copied)
    assert not (await sync(source, target, exclude=patterns)).changed


async def test_excluded_paths_survive_delete_and_metadata(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "file").write_text("copy")
    for root in (source, target):
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text(str(root))
        (root / "ignored.pyc").write_text(str(root))
    (source / ".git").chmod(0o700)
    (target / ".git").chmod(0o750)
    (target / "ignored.pyc").chmod(0o640)
    # This whole subtree is absent at source, but contains protected descendants.
    (target / "extra" / "nested").mkdir(parents=True)
    (target / "extra" / "nested" / "keep.pyc").write_text("keep")
    (target / "extra" / "nested" / "remove.txt").write_text("remove")
    (target / "unprotected").mkdir()
    (target / "unprotected" / "remove.txt").write_text("remove")
    before = {
        name: (target / name).lstat()
        for name in (".git", ".git/config", "ignored.pyc", "extra", "extra/nested/keep.pyc")
    }
    result = await sync(source, target, delete=True, exclude=(".git/", "*.pyc"))
    assert result.deleted == 3 and result.files == 1
    assert (target / "file").read_text() == "copy"
    assert (target / ".git" / "config").read_text() == str(target)
    assert (target / "ignored.pyc").read_text() == str(target)
    assert (target / "extra" / "nested" / "keep.pyc").read_text() == "keep"
    assert not (target / "extra" / "nested" / "remove.txt").exists()
    assert not (target / "unprotected").exists()
    for name, old in before.items():
        new = (target / name).lstat()
        assert (new.st_ino, new.st_mode, new.st_uid, new.st_gid) == (old.st_ino, old.st_mode, old.st_uid, old.st_gid)
    assert not (await sync(source, target, delete=True, exclude=(".git/", "*.pyc"))).changed


@pytest.mark.parametrize("direction", ["upload", "download"])
async def test_excluded_directories_are_not_scanned(protocol, tmp_path, monkeypatch, direction):
    source, target = tmp_path / "src", tmp_path / "dst"
    for root in (source, target):
        (root / ".git").mkdir(parents=True)
        os.mkfifo(root / ".git" / "pipe")
    # Destination-only protected subtree must not be scanned by remote remove either.
    (target / "extra" / ".git").mkdir(parents=True)
    os.mkfifo(target / "extra" / ".git" / "pipe")
    scan = os.scandir

    def guarded(path):
        assert not str(path).endswith("/.git"), path
        return scan(path)

    async def remote(method, *args):
        if method == Rsync.scan:
            assert not str(args[0]).endswith("/.git"), args
        return await protocol(method, *args)

    monkeypatch.setattr(os, "scandir", guarded)
    result = await getattr(Rsync, direction)(remote, source, target, exclude=(".git/",), delete=True)
    assert not result.changed
    assert (target / "extra" / ".git" / "pipe").exists()


async def test_excluded_special_files_and_type_conflicts(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    os.mkfifo(source / "ignored.pipe")
    (source / "cache").mkdir()
    (target / "cache").write_text("keep destination file")
    (source / "other").write_text("skip source file")
    (target / "other").mkdir()
    (target / "other" / "data").write_text("keep directory")
    result = await sync(source, target, exclude=("*.pipe", "cache/", "other/"), delete=True)
    assert not result.changed
    assert (target / "cache").read_text() == "keep destination file"
    assert (target / "other" / "data").read_text() == "keep directory"
    assert not (target / "ignored.pipe").exists()


async def test_directory_only_pattern_does_not_follow_links(sync, tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    (source / "cache").symlink_to("missing")
    result = await sync(source, target, exclude=("cache/",))
    assert result.symlinks == 1 and os.readlink(target / "cache") == "missing"
    (source / "cache").unlink()
    # A directory-only exclusion does not protect a destination symlink from deletion.
    assert (await sync(source, target, exclude=("cache/",), delete=True)).deleted == 1


@pytest.mark.parametrize("exclude", ["*.pyc", [None], [""], ["/"], ["../outside"], ["a//b"], ["a/./b"], ["bad\0name"]])
async def test_invalid_exclude_before_mutation(sync, tmp_path, exclude):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    with pytest.raises((ValueError, TypeError)):
        await sync(source, target, exclude=exclude)
    assert not target.exists()
