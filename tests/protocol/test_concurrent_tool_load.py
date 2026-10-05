"""Several Tool classes must transfer together on a fresh connection.

A load runs its import in a worker thread. Modules of one package import each
other through the package, so loads that overlap used to form a cyclic wait on
the import locks, and importlib reported a deadlock.
"""

import asyncio
from typing import Any

import pytest

from rmote.tools import facts
from rmote.tools.facts.collectors import (
    AptFacts,
    NetworkFacts,
    PacmanFacts,
    PythonFacts,
    SystemdFacts,
    SystemFacts,
)
from rmote.tools.facts.schema import Collector

pytestmark = pytest.mark.timeout(120)

SIBLINGS: list[Collector[Any]] = [PacmanFacts, AptFacts, SystemFacts, PythonFacts, NetworkFacts, SystemdFacts]


@pytest.mark.asyncio
async def test_siblings_of_one_package_load_together(isolated_remote):
    results = await asyncio.gather(*(isolated_remote(collector.collect) for collector in SIBLINGS))
    assert len(results) == len(SIBLINGS)
    assert all(result is not None for result in results)
    for collector, result in zip(SIBLINGS, results, strict=True):
        assert isinstance(result, collector.result_type)


@pytest.mark.asyncio
async def test_every_collector_of_the_registry_loads_together(isolated_remote):
    branches = await facts.fetch(isolated_remote, concurrency=16)
    assert set(branches) == {
        "apt",
        "cpu",
        "memory",
        "pacman",
        "system",
        "python",
        "network",
        "networkd",
        "storage",
        "systemd",
        "systemd_timesync",
        "systemd_resolved",
    }


@pytest.mark.asyncio
async def test_one_class_loads_once_for_concurrent_callers(isolated_remote):
    results = await asyncio.gather(*(isolated_remote(PythonFacts.collect) for _ in range(8)))
    assert len({result.executable for result in results}) == 1
