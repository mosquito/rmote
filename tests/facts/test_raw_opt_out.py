"""A caller can refuse the source texts of a branch and keep its parsed fields.

The texts are most of a snapshot: nine tenths of the processor branch and four
fifths of the memory branch. They stay by default, because a field the model
does not have can still be read from them.
"""

import pickle
import platform
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rmote.tools import facts
from rmote.tools.facts.collectors import CpuFacts, MemoryFacts, cgroup
from tests.support.tool_cases.loud_facts import LINES, LoudFacts

pytestmark = pytest.mark.timeout(60)

CPU_BLOCK = """\
processor\t: {index}
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 36
model name\t: AMD Ryzen AI 9 HX PRO 370 w/ Radeon 890M
stepping\t: 0
microcode\t: 0xb204011
cpu MHz\t\t: 1996.222
cache size\t: 1024 KB
physical id\t: 0
siblings\t: 16
core id\t\t: {index}
cpu cores\t: 8
flags\t\t: {flags}
bugs\t\t: sysret_ss_attrs spectre_v1 spectre_v2 retbleed smt_rsb srso
"""

# A real processor lists about two hundred flags, which is why the text of
# /proc/cpuinfo dominates the branch.
FLAGS = " ".join(f"feature_{index:03d}" for index in range(200))

MEMINFO = "".join(f"Field{index}:   {index * 1024} kB\n" for index in range(57))
MEMINFO = "MemTotal:       16360236 kB\nMemAvailable:   14438568 kB\n" + MEMINFO


def size(value: object) -> int:
    return len(pickle.dumps(value, pickle.HIGHEST_PROTOCOL))


@pytest.fixture
def linux_sources(monkeypatch):
    """Serve synthetic Linux sources to the processor and memory collectors."""
    cpuinfo = "\n".join(CPU_BLOCK.format(index=index, flags=FLAGS) for index in range(16))
    sources = {
        "/proc/cpuinfo": cpuinfo,
        "/proc/meminfo": MEMINFO,
        "/proc/self/cgroup": "0::/\n",
        "/sys/fs/cgroup/memory.max": "8192\n",
        "/sys/fs/cgroup/memory.current": "4096\n",
        "/sys/fs/cgroup/cpu.max": "100000 100000\n",
    }

    def read_cpu(path: Path) -> str | None:
        return sources.get(str(path))

    def read_memory(path: Path = Path("/proc/meminfo")) -> str | None:
        return sources.get(str(path))

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(CpuFacts, "read_text", staticmethod(read_cpu))
    monkeypatch.setattr(MemoryFacts, "read_text", staticmethod(read_memory))
    monkeypatch.setattr(cgroup, "read_text", read_cpu)
    return sources


def test_a_branch_without_its_texts_keeps_every_parsed_field(linux_sources):
    cpu, memory = CpuFacts.collect(), MemoryFacts.collect()
    lean_cpu, lean_memory = CpuFacts.collect(raw=False), MemoryFacts.collect(raw=False)

    assert cpu.raw["cpuinfo"] == linux_sources["/proc/cpuinfo"]
    assert lean_cpu.raw == lean_memory.raw == {}
    assert replace(cpu, raw={}) == lean_cpu
    assert replace(memory, raw={}) == lean_memory
    assert lean_memory.usage_bytes == 4096


def test_the_texts_are_most_of_a_snapshot(linux_sources):
    full = {"cpu": CpuFacts.collect(), "memory": MemoryFacts.collect()}
    lean = {"cpu": CpuFacts.collect(raw=False), "memory": MemoryFacts.collect(raw=False)}

    assert size(full["cpu"]) > 30000
    # The texts are the larger part of every branch that reads one.
    assert size(lean["cpu"]) * 8 < size(full["cpu"])
    assert size(lean["memory"]) * 2 < size(full["memory"])
    assert size(lean) * 5 < size(full)


@pytest.mark.asyncio
async def test_the_refusal_crosses_the_wire_for_a_call_per_collector(protocol):
    lean: Any = await facts.fetch(protocol, collectors=[LoudFacts], raw=False)
    assert lean["loud"].raw == {}
    assert lean["loud"].lines == LINES

    full: Any = await facts.fetch(protocol, collectors=[LoudFacts])
    assert full["loud"].raw["text"].count("\n") == LINES
    assert size(lean) * 10 < size(full)


@pytest.mark.asyncio
async def test_the_refusal_crosses_the_wire_for_one_remote_gather(protocol):
    lean: Any = await protocol(facts.gather, collectors=[LoudFacts], raw=False)
    assert lean["loud"].raw == {}
    assert lean["loud"].lines == LINES

    full: Any = await protocol(facts.gather, collectors=[LoudFacts])
    assert full["loud"].raw["text"].count("\n") == LINES


def test_a_collector_that_cannot_drop_its_texts_is_collected_as_it_is():
    from rmote.tools.facts.collectors import PythonFacts

    assert not facts.drops_raw(PythonFacts.collect)
    assert facts.drops_raw(CpuFacts.collect)
    assert facts.drops_raw(LoudFacts.collect)


@pytest.mark.asyncio
async def test_a_mixed_set_collects_without_the_keyword_where_it_is_missing():
    from rmote.tools.facts.collectors import PythonFacts

    branch: Any = await facts.gather(collectors=[PythonFacts, LoudFacts], raw=False)
    assert set(branch) == {"python", "loud"}
    assert branch["loud"].raw == {}
    assert branch["python"].version
