"""The SSH facts CLI must honor its cache policy across invocations.

The CLI calls one collector for each stale branch, so the fake peer answers
one call for each collector instead of one call for the whole batch.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from rmote.cache import CacheEntry, JSONCacheDir
from tests.facts.tools import CounterFacts, CounterInfo, FirstFacts, OverlapInfo


@pytest.mark.parametrize("options", [[], ["--max-age", "300"]])
def test_cli_reuses_cached_facts_and_can_force_refresh(tmp_path, monkeypatch, capsys, options):
    path = Path(__file__).parents[2] / "examples" / "ssh-facts.py"
    spec = importlib.util.spec_from_file_location("ssh_facts_example", path)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    monkeypatch.chdir(tmp_path)
    calls = {"first": 0, "second": 0}
    connections = {"first": 0, "second": 0}
    closed = {"first": 0, "second": 0}

    class Remote:
        def __init__(self, host: str) -> None:
            self.host = host

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed[self.host] += 1

        async def __call__(self, method):
            calls[self.host] += 1
            return CounterInfo(calls[self.host])

    async def connect(host, *, python):
        assert python == "python"
        connections[host] += 1
        await asyncio.sleep(0)
        return Remote(host)

    monkeypatch.setattr(example.Protocol, "from_ssh", connect)
    monkeypatch.setattr(example.facts, "DEFAULT_COLLECTORS", (CounterFacts,))
    command = [str(path), "--python", "python", "first", "second"]
    monkeypatch.setattr(sys, "argv", [*command, *options])
    assert example.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert calls == {"first": 1, "second": 1}
    assert connections == closed == {"first": 1, "second": 1}

    assert example.main() == 0
    assert json.loads(capsys.readouterr().out) == first
    assert calls == {"first": 1, "second": 1}
    assert connections == closed == {"first": 1, "second": 1}

    monkeypatch.setattr(sys, "argv", [*command, "--max-age", "0"])
    assert example.main() == 0
    refreshed = json.loads(capsys.readouterr().out)
    assert calls == {"first": 2, "second": 2}
    assert connections == closed == {"first": 2, "second": 2}
    assert refreshed == {host: {"counter": {"calls": 2}} for host in calls}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "connect", "collect"])
async def test_collectors_share_lazy_connection_and_cleanup(tmp_path, monkeypatch, failure):
    path = Path(__file__).parents[2] / "examples" / "ssh-facts.py"
    spec = importlib.util.spec_from_file_location("ssh_facts_example", path)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    monkeypatch.chdir(tmp_path)
    calls = []
    connections = 0
    closed = 0

    class Remote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            nonlocal closed
            closed += 1

        async def __call__(self, method):
            if failure == "collect":
                raise OSError("collection failed")
            key = method.__tool_class__.key
            calls.append(key)
            await asyncio.sleep(0)
            return CounterInfo(len(calls)) if key == "counter" else OverlapInfo(1, 42)

    async def connect(host, *, python):
        nonlocal connections
        connections += 1
        await asyncio.sleep(0)
        if failure == "connect":
            raise OSError("connection failed")
        return Remote()

    monkeypatch.setattr(example.Protocol, "from_ssh", connect)
    monkeypatch.setattr(example.facts, "DEFAULT_COLLECTORS", (CounterFacts, FirstFacts))
    if failure:
        with pytest.raises(OSError, match="failed"):
            await example.host_facts("host", max_age=60)
        assert connections == 1
        assert closed == (0 if failure == "connect" else 1)
        return

    await example.host_facts("host", max_age=60)
    assert sorted(calls) == ["counter", "first"]
    assert connections == closed == 1
    calls.clear()
    await JSONCacheDir(".cache/facts").delete("host", "counter")
    await example.host_facts("host", max_age=60)
    assert calls == ["counter"]
    assert connections == closed == 2

    calls.clear()
    cache = JSONCacheDir(".cache/facts")
    await cache.delete("host", "counter")
    await cache.set("host", "counter", CacheEntry(CounterInfo(0), 0.0))
    await example.host_facts("host", max_age=60)
    assert calls == ["counter"]
    assert connections == closed == 3
