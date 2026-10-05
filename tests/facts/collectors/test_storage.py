"""Storage facts must read the kernel sources exactly and survive their absence."""

import os
from pathlib import Path

import pytest

from rmote.cache import Cache
from rmote.tools import facts
from rmote.tools.facts.collectors import StorageFacts
from rmote.tools.facts.collectors.storage import SYSFS_SECTOR, StorageInfo

MOUNTINFO = """\
34 44 0:29 / /run rw,nosuid,nodev shared:13 - tmpfs tmpfs rw,size=3272048k,mode=755
44 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
41 44 0:7 / /dev rw,nosuid shared:2 - devtmpfs devtmpfs rw,size=7335656k,mode=755
25 44 0:23 / /proc ro,nosuid,nodev,noexec,relatime shared:5 - proc proc rw
60 44 8:1 /sub /mnt/with\\040space ro,relatime - vfat /dev/sda1 ro,fmask=0022
61 44 0:55 / /mnt/net rw,relatime - nfs4 server:/export rw,vers=4.2
"""

MOUNTS = """\
/dev/sda2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid 0 0
/dev/sda1 /mnt/with\\040space vfat ro,relatime 0 0
"""

SWAPS = """\
Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority
/swapfile                               file\t\t2097148\t\t512\t\t-2
/dev/sda3                               partition\t4194304\t\t0\t\t-3
"""

FSTAB = """\
# /etc/fstab
UUID=1234-5678  /          ext4  defaults  0  1
/dev/sda1       /boot/efi  vfat  umask=0077  0  2
tmpfs           /tmp       tmpfs  # trailing comment
broken line
"""


def test_mountinfo_is_parsed_with_escapes_and_optional_fields():
    records = StorageFacts.parse_mountinfo(MOUNTINFO)
    assert [record["target"] for record in records] == [
        "/run",
        "/",
        "/dev",
        "/proc",
        "/mnt/with space",
        "/mnt/net",
    ]
    root = records[1]
    assert root["fstype"] == "ext4" and root["source"] == "/dev/sda2" and root["device"] == "8:2"
    # An optional field is present for some mounts and absent for others, so
    # the separator decides where the filesystem fields begin.
    shadowed = records[4]
    assert shadowed["root"] == "/sub"
    assert shadowed["fstype"] == "vfat"
    assert shadowed["super_options"] == ["ro", "fmask=0022"]
    assert records[5]["source"] == "server:/export"


def test_damaged_mountinfo_lines_are_skipped():
    assert StorageFacts.parse_mountinfo("") == []
    assert StorageFacts.parse_mountinfo("not a record\n") == []
    assert StorageFacts.parse_mountinfo("34 44 0:29 / /run rw - tmpfs\n") == []


def test_octal_escapes_decode_only_complete_sequences():
    assert StorageFacts.unescape("/mnt/with\\040space") == "/mnt/with space"
    assert StorageFacts.unescape("tab\\011end") == "tab\tend"
    assert StorageFacts.unescape("plain") == "plain"
    assert StorageFacts.unescape("keep\\x41") == "keep\\x41"
    assert StorageFacts.unescape("tail\\04") == "tail\\04"


def test_mounts_file_is_the_fallback_without_device_numbers(tmp_path):
    (tmp_path / "mounts").write_text(MOUNTS)
    mounts, raw = StorageFacts.read_mounts(tmp_path / "absent", tmp_path / "mounts")
    assert mounts is not None
    assert [mount.target for mount in mounts] == ["/", "/proc", "/mnt/with space"]
    assert all(mount.device is None and mount.root == "/" for mount in mounts)
    assert mounts[2].read_only is True and mounts[0].read_only is False
    assert mounts[1].pseudo is True and mounts[1].usage is None
    assert set(raw) == {"mounts"}


def test_absent_mount_table_reports_no_section(tmp_path):
    mounts, raw = StorageFacts.read_mounts(tmp_path / "absent", tmp_path / "also-absent")
    assert mounts is None and raw == {}


def test_pseudo_filesystems_keep_their_place_without_usage(tmp_path):
    (tmp_path / "mountinfo").write_text(MOUNTINFO)
    mounts, raw = StorageFacts.read_mounts(tmp_path / "mountinfo", tmp_path / "absent")
    assert mounts is not None
    assert set(raw) == {"mountinfo"}
    pseudo = {mount.target: mount.pseudo for mount in mounts}
    assert pseudo == {
        "/run": False,
        "/": False,
        "/dev": False,
        "/proc": True,
        "/mnt/with space": False,
        "/mnt/net": False,
    }
    # Space is read for real filesystems only, and a missing target is refused
    # without failing the branch.
    assert all(mount.usage is None for mount in mounts if mount.pseudo)


def test_usage_reports_bytes_and_refuses_a_missing_target(tmp_path):
    usage = StorageFacts.usage(str(tmp_path))
    assert usage is not None
    status = os.statvfs(tmp_path)
    unit = status.f_frsize or status.f_bsize
    assert usage.total_bytes == status.f_blocks * unit
    assert usage.available_bytes <= usage.free_bytes <= usage.total_bytes
    assert StorageFacts.usage(str(tmp_path / "absent")) is None


def test_swaps_are_reported_in_bytes(tmp_path):
    (tmp_path / "swaps").write_text(SWAPS)
    areas, text = StorageFacts.read_swaps(tmp_path / "swaps")
    assert text == SWAPS
    assert areas is not None and len(areas) == 2
    assert areas[0].path == "/swapfile" and areas[0].kind == "file"
    assert areas[0].size_bytes == 2097148 * 1024
    assert areas[0].used_bytes == 512 * 1024
    assert areas[0].priority == -2
    assert areas[1].kind == "partition" and areas[1].used_bytes == 0
    assert StorageFacts.read_swaps(tmp_path / "absent") == (None, None)


def test_swaps_with_only_a_header_are_empty_not_missing(tmp_path):
    (tmp_path / "swaps").write_text(SWAPS.splitlines()[0] + "\n")
    areas, text = StorageFacts.read_swaps(tmp_path / "swaps")
    assert areas == [] and text is not None


def test_fstab_entries_keep_defaults_and_skip_comments(tmp_path):
    (tmp_path / "fstab").write_text(FSTAB)
    entries, text = StorageFacts.read_fstab(tmp_path / "fstab")
    assert text == FSTAB
    assert entries is not None
    assert [entry["target"] for entry in entries] == ["/", "/boot/efi", "/tmp"]
    assert entries[0]["options"] == "defaults" and entries[0]["pass"] == "1"
    assert entries[2]["options"] == "defaults" and entries[2]["dump"] == "0"
    assert StorageFacts.read_fstab(tmp_path / "absent") == (None, None)


def build_sysfs(root: Path) -> None:
    """Write a block device tree shaped like /sys/block."""
    disk = root / "sda"
    (disk / "queue").mkdir(parents=True)
    (disk / "device").mkdir()
    (disk / "size").write_text("500118192\n")
    (disk / "removable").write_text("0\n")
    (disk / "queue" / "rotational").write_text("1\n")
    (disk / "queue" / "logical_block_size").write_text("512\n")
    (disk / "device" / "model").write_text("TEST DISK   \n")
    for name, sectors in (("sda1", "1048576"), ("sda2", "499068928")):
        partition = disk / name
        partition.mkdir()
        (partition / "partition").write_text("1\n")
        (partition / "size").write_text(sectors + "\n")
    # A child directory without a partition attribute is not a partition.
    (disk / "holders").mkdir()
    empty = root / "loop0"
    (empty / "queue").mkdir(parents=True)
    (empty / "size").write_text("0\n")
    (empty / "queue" / "rotational").write_text("0\n")


def test_block_devices_report_bytes_and_their_partitions(tmp_path):
    build_sysfs(tmp_path)
    devices = StorageFacts.read_devices(tmp_path)
    assert devices is not None and set(devices) == {"sda", "loop0"}
    disk = devices["sda"]
    assert disk.size_bytes == 500118192 * SYSFS_SECTOR
    assert disk.logical_block_size == 512
    assert disk.rotational is True and disk.removable is False
    assert disk.model == "TEST DISK"
    assert disk.partitions == {"sda1": 1048576 * SYSFS_SECTOR, "sda2": 499068928 * SYSFS_SECTOR}
    empty = devices["loop0"]
    assert empty.size_bytes == 0 and empty.rotational is False
    assert empty.model is None and empty.removable is None
    assert empty.partitions == {}


def test_unreadable_sysfs_and_damaged_attributes(tmp_path):
    assert StorageFacts.read_devices(tmp_path / "absent") is None
    (tmp_path / "sdb").mkdir()
    (tmp_path / "sdb" / "size").write_text("not a number\n")
    devices = StorageFacts.read_devices(tmp_path)
    assert devices is not None and devices["sdb"].size_bytes is None


def test_collect_on_this_host_matches_its_platform():
    result = StorageFacts.collect()
    assert isinstance(result, StorageInfo)
    # The space of the root filesystem is portable; the mount table is not.
    assert result.root is not None and result.root.total_bytes > 0
    assert result.root.available_bytes <= result.root.free_bytes <= result.root.total_bytes
    if result.available:
        assert result.mounts
        assert any(mount.target == "/" for mount in result.mounts)
        assert "mountinfo" in result.raw or "mounts" in result.raw
    else:
        assert result.mounts is None and result.devices is None


@pytest.mark.asyncio
async def test_storage_branch_travels_and_validates(protocol):
    branch = await facts.fetch(protocol, sections=["storage"])
    assert set(branch) == {"storage"}
    assert isinstance(branch["storage"], StorageInfo)
    local = Cache[object](versions={StorageFacts.key: StorageFacts.version})
    local.update(branch)
    assert local.stale() == ()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_storage_in_a_container_reports_its_own_root(facts_docker_protocol):
    branch = await facts.fetch(facts_docker_protocol, sections=["storage"])
    storage = branch["storage"]
    assert storage.available is True
    assert storage.mounts
    root = next(mount for mount in storage.mounts if mount.target == "/")
    assert root.usage is not None and root.usage.total_bytes > 0
    # The portable field reports the same filesystem as the / mount. Only the
    # total is compared: the free space can change between the two calls.
    assert storage.root is not None
    assert storage.root.total_bytes == root.usage.total_bytes
    assert any(mount.pseudo for mount in storage.mounts)
    # The container mounts /sys read only, which the options must show.
    assert any(mount.target == "/sys" and mount.read_only for mount in storage.mounts)


def test_a_target_without_a_mount_table_still_reports_the_root(monkeypatch):
    """os.statvfs answers on every POSIX target, so the space is reported."""
    monkeypatch.setattr(StorageFacts, "read_mounts", staticmethod(lambda *args: (None, {})))
    result = StorageFacts.collect()

    assert result.available is False
    assert result.mounts is None
    assert result.root is not None and result.root.total_bytes > 0


def test_a_root_that_cannot_be_queried_reports_no_space(monkeypatch):
    monkeypatch.setattr(StorageFacts, "usage", staticmethod(lambda target: None))
    assert StorageFacts.collect().root is None
