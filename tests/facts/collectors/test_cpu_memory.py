"""Processor and memory facts must read their sources exactly.

Both branches describe the machine and what the running process may use, so
the tests cover the installed values, the limited values and every absent
source.
"""

import os
import platform
from pathlib import Path

import pytest

from rmote.cache import Cache
from rmote.tools import facts
from rmote.tools.facts.collectors import CpuFacts, MemoryFacts, posix
from rmote.tools.facts.collectors import cgroup as cgroup_module
from rmote.tools.facts.collectors.cpu import CpuInfo
from rmote.tools.facts.collectors.memory import MemoryInfo

CPUINFO = """\
processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 36
model name\t: AMD Ryzen AI 9 HX PRO 370 w/ Radeon 890M
stepping\t: 0
microcode\t: 0xb204011
cpu MHz\t\t: 1996.222
cache size\t: 1024 KB
physical id\t: 0
siblings\t: 8
core id\t\t: 0
cpu cores\t: 8
flags\t\t: fpu vme de pse tsc msr
bugs\t\t: sysret_ss_attrs spectre_v1 spectre_v2

processor\t: 1
vendor_id\t: AuthenticAMD
model name\t: AMD Ryzen AI 9 HX PRO 370 w/ Radeon 890M
cpu MHz\t\t: 2048.000
physical id\t: 0
cpu cores\t: 8
flags\t\t: fpu vme de pse tsc msr
"""

MEMINFO = """\
MemTotal:       16360236 kB
MemFree:         5351696 kB
MemAvailable:   14438568 kB
Buffers:           49684 kB
Cached:          9571884 kB
SwapCached:            0 kB
Shmem:            131072 kB
SwapTotal:       2097148 kB
SwapFree:        2096000 kB
CommitLimit:     8183264 kB
Committed_AS:    1941952 kB
HugePages_Total:       2
HugePages_Free:        1
HugePages_Rsvd:        0
Hugepagesize:       2048 kB
DirectMap4k:      401344 kB
"""


def build_sysfs(root: Path, *, offline: str = "8-23", smt: str = "0", cpufreq: bool = False) -> Path:
    """Write a processor tree shaped like /sys/devices/system/cpu."""
    (root / "online").write_text("0-7\n")
    (root / "present").write_text("0-7\n")
    (root / "possible").write_text("0-23\n")
    (root / "offline").write_text(offline + "\n")
    (root / "smt").mkdir()
    (root / "smt" / "active").write_text(smt + "\n")
    vulnerabilities = root / "vulnerabilities"
    vulnerabilities.mkdir()
    (vulnerabilities / "meltdown").write_text("Not affected\n")
    (vulnerabilities / "spectre_v2").write_text("Mitigation: Enhanced IBRS\n")
    cache = root / "cpu0" / "cache"
    for index, (level, kind, size, shared) in enumerate(
        [("1", "Data", "64K", "0"), ("1", "Instruction", "64K", "0"), ("3", "Unified", "16384K", "0-7")]
    ):
        entry = cache / f"index{index}"
        entry.mkdir(parents=True)
        (entry / "level").write_text(level + "\n")
        (entry / "type").write_text(kind + "\n")
        (entry / "size").write_text(size + "\n")
        (entry / "shared_cpu_list").write_text(shared + "\n")
    if cpufreq:
        policy = root / "cpu0" / "cpufreq"
        policy.mkdir(parents=True)
        (policy / "scaling_governor").write_text("performance\n")
        (policy / "scaling_driver").write_text("acpi-cpufreq\n")
        (policy / "cpuinfo_min_freq").write_text("400000\n")
        (policy / "cpuinfo_max_freq").write_text("5100000\n")
    return root


def test_cpuinfo_keeps_the_first_block_and_sorts_its_flags():
    records = CpuFacts.parse_cpuinfo(CPUINFO)
    assert len(records) == 2
    first = records[0]
    assert first["model name"] == "AMD Ryzen AI 9 HX PRO 370 w/ Radeon 890M"
    assert first["vendor_id"] == "AuthenticAMD"
    assert first["cpu family"] == "26" and first["stepping"] == "0"
    assert first["microcode"] == "0xb204011"
    # The second block reports another frequency, which is why only the first
    # block describes the processor.
    assert records[1]["cpu MHz"] == "2048.000"


def test_empty_and_damaged_cpuinfo_give_no_records():
    assert CpuFacts.parse_cpuinfo("") == []
    assert CpuFacts.parse_cpuinfo("no separator here\n") == []


def test_kernel_ranges_are_counted():
    assert CpuFacts.count_range("0-7") == 8
    assert CpuFacts.count_range("0") == 1
    assert CpuFacts.count_range("0-3,8,10-11") == 7
    assert CpuFacts.count_range("") is None
    assert CpuFacts.count_range(None) is None
    assert CpuFacts.count_range("broken") is None


def test_cache_sizes_convert_to_bytes():
    assert CpuFacts.parse_size("64K") == 64 * 1024
    assert CpuFacts.parse_size("16384K") == 16384 * 1024
    assert CpuFacts.parse_size("2M") == 2 * 1024**2
    assert CpuFacts.parse_size("512") == 512
    assert CpuFacts.parse_size("64 KB") == 64 * 1024
    assert CpuFacts.parse_size("huge") is None
    assert CpuFacts.parse_size(None) is None


def test_topology_derives_sockets_cores_and_threads(tmp_path):
    build_sysfs(tmp_path)
    topology = CpuFacts.read_topology(CpuFacts.parse_cpuinfo(CPUINFO), tmp_path)
    assert topology.logical == 8
    assert topology.sockets == 1
    assert topology.cores_per_socket == 8
    assert topology.threads_per_core == 1
    assert topology.smt is False
    assert topology.online == "0-7" and topology.offline == "8-23"
    assert topology.possible == "0-23"


def test_topology_without_identifiers_reports_no_guess(tmp_path):
    build_sysfs(tmp_path, smt="1")
    topology = CpuFacts.read_topology([{"processor": "0"}, {"processor": "1"}], tmp_path)
    assert topology.logical == 8
    assert topology.sockets is None
    assert topology.cores_per_socket is None
    assert topology.threads_per_core is None
    assert topology.smt is True


def test_caches_and_mitigations_are_read(tmp_path):
    build_sysfs(tmp_path)
    caches = CpuFacts.read_caches(tmp_path)
    assert caches is not None
    assert [(cache.level, cache.kind, cache.size_bytes) for cache in caches] == [
        (1, "Data", 64 * 1024),
        (1, "Instruction", 64 * 1024),
        (3, "Unified", 16384 * 1024),
    ]
    assert caches[2].shared_with == "0-7"
    assert CpuFacts.read_vulnerabilities(tmp_path) == {
        "meltdown": "Not affected",
        "spectre_v2": "Mitigation: Enhanced IBRS",
    }


def test_absent_cache_and_mitigation_directories(tmp_path):
    assert CpuFacts.read_caches(tmp_path) is None
    assert CpuFacts.read_vulnerabilities(tmp_path) is None


def test_frequency_reports_the_policy_or_only_the_current_value(tmp_path):
    first = CpuFacts.parse_cpuinfo(CPUINFO)[0]
    without = CpuFacts.read_frequency(first, tmp_path)
    assert without.current_mhz == pytest.approx(1996.222)
    assert without.governor is None and without.maximum_khz is None
    build_sysfs(tmp_path, cpufreq=True)
    policy = CpuFacts.read_frequency(first, tmp_path)
    assert policy.governor == "performance" and policy.driver == "acpi-cpufreq"
    assert policy.minimum_khz == 400000 and policy.maximum_khz == 5100000
    assert CpuFacts.read_frequency({}, tmp_path).current_mhz is None
    assert CpuFacts.read_frequency({"cpu MHz": "unknown"}, tmp_path).current_mhz is None


def test_meminfo_values_become_bytes():
    values = MemoryFacts.parse(MEMINFO)
    assert values["MemTotal"] == 16360236 * 1024
    assert values["MemAvailable"] == 14438568 * 1024
    # A plain count carries no unit and is not scaled.
    assert values["HugePages_Total"] == 2
    assert MemoryFacts.parse("") == {}
    assert MemoryFacts.parse("Broken line\nAlso: not a number kB\n") == {}


def test_swap_and_hugepages_are_derived():
    values = MemoryFacts.parse(MEMINFO)
    swap = MemoryFacts.read_swap(values)
    assert swap is not None
    assert swap.total_bytes == 2097148 * 1024
    assert swap.used_bytes == (2097148 - 2096000) * 1024
    assert swap.cached_bytes == 0
    pages = MemoryFacts.read_hugepages(values)
    assert pages is not None
    assert pages.size_bytes == 2048 * 1024 and pages.total == 2 and pages.free == 1
    assert MemoryFacts.read_swap({}) is None
    assert MemoryFacts.read_hugepages({}) is None


def test_absent_meminfo_reports_no_branch(tmp_path):
    assert MemoryFacts.read_text(tmp_path / "absent") is None


def test_cgroup_limit_reports_the_strictest_visible_group(tmp_path):
    root = tmp_path / "cgroup"
    (root / "payload" / "deep").mkdir(parents=True)
    process = tmp_path / "self-cgroup"
    process.write_text("0::/payload/deep\n")
    # A parent limit applies to its children, and a group that declares
    # nothing hides no ancestor.
    (root / "payload" / "memory.max").write_text("2147483648\n")
    assert cgroup_module.own_path(root, process) == root / "payload" / "deep"
    assert cgroup_module.bytes_limit("memory.max", root, process) == 2147483648
    # A child that declares no limit still reports the limit of the parent.
    (root / "payload" / "deep" / "memory.max").write_text("max\n")
    assert cgroup_module.bytes_limit("memory.max", root, process) == 2147483648
    # The strictest group wins, whichever end of the chain declares it.
    (root / "payload" / "deep" / "memory.max").write_text("536870912\n")
    assert cgroup_module.bytes_limit("memory.max", root, process) == 536870912
    (root / "payload" / "memory.max").write_text("268435456\n")
    assert cgroup_module.bytes_limit("memory.max", root, process) == 268435456
    # Malformed text declares no limit and does not hide the parent.
    (root / "payload" / "deep" / "memory.max").write_text("broken\n")
    assert cgroup_module.bytes_limit("memory.max", root, process) == 268435456
    assert cgroup_module.bytes_limit("absent.max", root, process) is None


def test_cgroup_limit_stops_at_the_namespace_root(tmp_path):
    root = tmp_path / "cgroup"
    (root / "payload").mkdir(parents=True)
    process = tmp_path / "self-cgroup"
    process.write_text("0::/payload\n")
    # A group above the visible root belongs to another namespace. Its limit
    # is enforced but never reported, so None does not prove freedom.
    (tmp_path / "memory.max").write_text("1048576\n")
    assert cgroup_module.bytes_limit("memory.max", root, process) is None
    (root / "memory.max").write_text("4294967296\n")
    assert cgroup_module.bytes_limit("memory.max", root, process) == 4294967296


def test_cgroup_usage_describes_the_own_group_only(tmp_path):
    root = tmp_path / "cgroup"
    (root / "payload" / "deep").mkdir(parents=True)
    process = tmp_path / "self-cgroup"
    process.write_text("0::/payload/deep\n")
    # The parent counts the processes of its other children, so its usage is
    # not the usage of this process's group.
    (root / "payload" / "memory.current").write_text("999999\n")
    assert cgroup_module.own_bytes("memory.current", root, process) is None
    (root / "payload" / "deep" / "memory.current").write_text("131072\n")
    assert cgroup_module.own_bytes("memory.current", root, process) == 131072
    assert cgroup_module.own_value("absent.current", root, process) is None


def test_cgroup_version_one_and_absent_sources(tmp_path):
    root = tmp_path / "cgroup"
    root.mkdir()
    process = tmp_path / "self-cgroup"
    # Version 1 lists controllers and is not read.
    process.write_text("4:memory:/payload\n3:cpu,cpuacct:/payload\n")
    assert cgroup_module.own_path(root, process) is None
    assert cgroup_module.own_path(root, tmp_path / "absent") is None
    assert list(cgroup_module.values("memory.max", root, process)) == []
    assert cgroup_module.bytes_limit("memory.max", root, process) is None
    assert cgroup_module.own_bytes("memory.current", root, process) is None


def test_cpu_quota_expresses_the_limit_as_processors(monkeypatch):
    cases = [
        (["200000 100000"], 2.0),
        (["50000 100000"], 0.5),
        (["max 100000"], None),
        (["broken"], None),
        (["100000 0"], None),
        ([], None),
    ]
    for lines, expected in cases:
        monkeypatch.setattr("rmote.tools.facts.collectors.cpu.cgroup.values", lambda name, lines=lines: iter(lines))
        assert CpuFacts.read_quota() == expected


def test_cpu_quota_compares_processors_not_raw_quotas(monkeypatch):
    # The child allows two processors with a 100 ms period; the parent allows
    # a tenth of one with a 1 s period. Raw quotas would name the parent the
    # looser group, so the pairs are divided before the comparison.
    chain = ["200000 100000", "100000 1000000"]
    monkeypatch.setattr("rmote.tools.facts.collectors.cpu.cgroup.values", lambda name: iter(chain))
    assert CpuFacts.read_quota() == 0.1
    # A group that declares no limit leaves the strictest of the others.
    chain = ["max 100000", "200000 100000", "broken"]
    assert CpuFacts.read_quota() == 2.0


def test_usable_count_follows_the_affinity_of_this_process():
    usable = CpuFacts.usable_count()
    assert usable is not None and usable >= 1
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        assert usable == len(affinity(0))
    else:
        assert usable == os.cpu_count()


def test_collect_on_this_host_matches_its_platform():
    cpu = CpuFacts.collect()
    memory = MemoryFacts.collect()
    assert isinstance(cpu, CpuInfo) and isinstance(memory, MemoryInfo)
    assert cpu.architecture == platform.machine()
    assert cpu.usable is not None
    if cpu.available:
        assert cpu.topology is not None and cpu.topology.logical
        assert "cpuinfo" in cpu.raw
        # An arm64 kernel publishes no model name, so the model follows the
        # table exactly: present when the table names it, absent when not.
        records = CpuFacts.parse_cpuinfo(cpu.raw["cpuinfo"])
        named = bool(records) and bool(records[0].get("model name") or records[0].get("Model"))
        assert bool(cpu.model) is named
    else:
        # No kernel table, but sysconf counts the online processors.
        assert cpu.topology is not None and cpu.topology.logical
        assert cpu.model is None
    if memory.available:
        assert memory.total_bytes and memory.available_bytes
        assert memory.total_bytes >= memory.available_bytes
        assert "meminfo" in memory.raw
    else:
        # No kernel table, but sysconf reports the memory of the machine.
        assert memory.total_bytes
        assert memory.available_bytes is None


@pytest.mark.asyncio
async def test_both_branches_travel_and_validate(protocol):
    branch = await facts.fetch(protocol, sections=["cpu", "memory"])
    assert set(branch) == {"cpu", "memory"}
    assert isinstance(branch["cpu"], CpuInfo)
    assert isinstance(branch["memory"], MemoryInfo)
    local = Cache[object](versions={CpuFacts.key: CpuFacts.version, MemoryFacts.key: MemoryFacts.version})
    local.update(branch)
    assert local.stale() == ()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_container_reports_the_machine_and_its_own_limits(facts_docker_protocol):
    branch = await facts.fetch(facts_docker_protocol, sections=["cpu", "memory"])
    cpu, memory = branch["cpu"], branch["memory"]
    assert cpu.available is True
    assert cpu.topology is not None and cpu.topology.logical
    assert cpu.usable is not None and cpu.usable >= 1
    assert cpu.vulnerabilities is not None
    assert memory.available is True
    assert memory.total_bytes and memory.total_bytes > 0
    assert memory.used_bytes is not None and memory.used_bytes <= memory.total_bytes


def test_sysconf_answers_a_known_name_and_refuses_an_unknown_one():
    assert posix.sysconf("SC_PAGE_SIZE") == os.sysconf("SC_PAGE_SIZE")
    # The set of names differs between systems, and an unknown one raises.
    assert posix.sysconf("SC_THIS_NAME_DOES_NOT_EXIST") is None


def test_an_unspecified_limit_is_not_a_value(monkeypatch):
    monkeypatch.setattr(os, "sysconf", lambda name: -1)
    assert posix.sysconf("SC_PAGE_SIZE") is None


def test_memory_without_the_kernel_table_reports_what_sysconf_gives(monkeypatch):
    """A target without /proc/meminfo still reports the memory it has."""
    monkeypatch.setattr(MemoryFacts, "read_text", staticmethod(lambda *args: None))
    result = MemoryFacts.collect()

    assert result.available is False
    assert result.total_bytes == os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    # Only the pages are portable; the rest of the table has no such call.
    assert result.available_bytes is None and result.swap is None and result.raw == {}


def test_memory_without_sysconf_names_reports_nothing(monkeypatch):
    monkeypatch.setattr(posix, "sysconf", lambda name: None)
    result = MemoryFacts.portable()

    assert result.available is False
    assert result.total_bytes is None and result.free_bytes is None


def test_cpu_without_the_kernel_table_reports_the_online_count(monkeypatch):
    monkeypatch.setattr(CpuFacts, "read_text", staticmethod(lambda path: None))
    result = CpuFacts.collect()

    assert result.available is False
    assert result.architecture == platform.machine()
    assert result.usable == CpuFacts.usable_count()
    assert result.topology is not None
    assert result.topology.logical == os.sysconf("SC_NPROCESSORS_ONLN")
    # Sockets, cores and the kernel ranges have no portable source.
    assert result.topology.sockets is None and result.topology.online is None
    assert result.model is None and result.raw == {}


def test_cpu_without_the_online_count_reports_no_topology(monkeypatch):
    monkeypatch.setattr(posix, "sysconf", lambda name: None)
    assert CpuFacts.portable_topology() is None
