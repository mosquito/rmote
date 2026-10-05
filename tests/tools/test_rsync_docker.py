"""Real destination ownership and directory transfer without shared mounts."""

import json
import os
from typing import cast

import pytest

from rmote.tools import Exec, Rsync

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


async def test_remote_tree_ownership_and_download(docker_protocol, tmp_path):
    source = tmp_path / "source"
    (source / "empty").mkdir(parents=True)
    (source / "file").write_bytes(b"AAAABBBBCCCC")
    (source / "file").chmod(0o751)
    (source / "link").symlink_to("file")
    remote = "/tmp/rsync-destination"
    await docker_protocol(Exec.command, "mkdir", remote)
    await docker_protocol(Exec.command, "chgrp", "nogroup", remote)
    await docker_protocol(Exec.command, "chmod", "2770", remote)

    async def metadata() -> dict[str, list[int]]:
        output = await docker_protocol(
            Exec.command,
            "python3",
            "-c",
            "import os,json; from pathlib import Path; "
            f"p=Path({remote!r}); "
            "print(json.dumps({str(q.relative_to(p)): "
            "(q.lstat().st_uid,q.lstat().st_gid,q.lstat().st_mode & 4095) "
            'for q in [p,*p.rglob("*")]}))',
            capture_output=True,
        )
        return cast(dict[str, list[int]], json.loads(output.stdout))

    await Rsync.upload(docker_protocol, source, remote, preserve_mode=False)
    initial = await metadata()
    assert initial["file"][:2] == [0, 65534]
    assert initial["empty"][:2] == [0, 65534]
    assert initial["link"][:2] == [0, 65534]
    assert initial["."][2] == 0o2770
    assert initial["file"][2] == 0o600

    explicit = await Rsync.upload(docker_protocol, source, remote, owner="nobody", group="nogroup")
    assert explicit.changed and explicit.transferred == 0
    changed = await metadata()
    assert all(item[:2] == [65534, 65534] for item in changed.values())
    assert changed["file"][2] == 0o751
    assert not (await Rsync.upload(docker_protocol, source, remote, owner="nobody", group="nogroup")).changed

    (source / "file").write_bytes(b"AAAAXXXXCCCC")
    updated = await Rsync.upload(docker_protocol, source, remote, block_size=4)
    assert updated.transferred == 4
    # No owner request preserves ownership of an existing destination file.
    assert (await metadata())["file"][:2] == [65534, 65534]
    target = tmp_path / "download"
    downloaded = await Rsync.download(docker_protocol, remote, target)
    assert downloaded.changed and (target / "file").read_bytes() == b"AAAAXXXXCCCC"
    assert (target / "file").stat().st_uid == os.geteuid()
    assert (target / "empty").is_dir() and os.readlink(target / "link") == "file"
    (target / "extra").write_text("extra")
    assert (await Rsync.download(docker_protocol, remote, target, delete=True)).deleted == 1


async def test_excludes_across_container_boundary(docker_protocol, tmp_path):
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "config").write_text("local git")
    (source / "keep.txt").write_text("copy")
    (source / "skip.pyc").write_text("local cache")
    remote = "/tmp/rsync-excludes"
    await docker_protocol(
        Exec.command,
        "python3",
        "-c",
        "from pathlib import Path; "
        f"root=Path({remote!r}); "
        'names=[".git/config", "skip.pyc", "extra/deep/keep.pyc", "extra/remove.txt", "remove.txt"]; '
        '[( (root/name).parent.mkdir(parents=True,exist_ok=True), (root/name).write_text("remote")) for name in names]',
    )
    patterns = (".git/", "*.pyc")
    result = await Rsync.upload(
        docker_protocol,
        source,
        remote,
        exclude=patterns,
        delete=True,
        owner="nobody",
        group="nogroup",
    )
    assert result.files == 1 and result.deleted == 2
    output = await docker_protocol(
        Exec.command,
        "python3",
        "-c",
        "import json; from pathlib import Path; "
        f"root=Path({remote!r}); "
        "print(json.dumps({str(p.relative_to(root)):(p.read_text(),p.stat().st_uid) "
        'for p in root.rglob("*") if p.is_file()}))',
        capture_output=True,
    )
    assert json.loads(output.stdout) == {
        ".git/config": ["remote", 0],
        "skip.pyc": ["remote", 0],
        "extra/deep/keep.pyc": ["remote", 0],
        "keep.txt": ["copy", 65534],
    }
    target = tmp_path / "download"
    (target / ".git").mkdir(parents=True)
    (target / ".git" / "config").write_text("destination git")
    (target / "extra-local").mkdir()
    (target / "extra-local" / "protected.pyc").write_text("destination cache")
    (target / "extra-local" / "remove.txt").touch()
    result = await Rsync.download(docker_protocol, remote, target, exclude=patterns, delete=True)
    assert result.files == 1 and result.deleted == 1
    assert (target / "keep.txt").read_text() == "copy"
    assert (target / ".git" / "config").read_text() == "destination git"
    assert (target / "extra-local" / "protected.pyc").read_text() == "destination cache"
    assert not (target / "skip.pyc").exists()
    assert not (target / "extra" / "deep" / "keep.pyc").exists()
    assert not (target / "extra-local" / "remove.txt").exists()
