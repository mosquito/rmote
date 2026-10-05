"""The bundle of a package travels once for a connection.

Every Tool class of a package carries the same sources. The peer keeps what it
received, so the classes that follow announce themselves with an empty bundle.
"""

import asyncio
import importlib
import sys
from typing import Any

import pytest

from rmote.protocol import Flags, Protocol


@pytest.fixture
def packages(tmp_path, monkeypatch):
    """Two independent transferable packages, three Tool classes each."""
    for name in ("bundle_probe", "other_probe"):
        root = tmp_path / name
        root.mkdir()
        (root / "__init__.py").write_text(f'''__tool_package__ = "{name}"

from rmote.protocol import Tool

from .helper import mark


class First(Tool):
    def value(self) -> str:
        return mark("first")


class Second(Tool):
    def value(self) -> str:
        return mark("second")


class Third(Tool):
    def value(self) -> str:
        return mark("third")
''')
        (root / "helper.py").write_text("""def mark(name: str) -> str:
    return name + ":" + name
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    first = importlib.import_module("bundle_probe")
    second = importlib.import_module("other_probe")
    yield first, second
    for module in list(sys.modules):
        if module.startswith(("bundle_probe", "other_probe")):
            del sys.modules[module]


@pytest.fixture
def synced(monkeypatch):
    """Record the definition of every tool a protocol announces."""
    sent: list[dict[str, Any]] = []
    original = Protocol._call

    async def recorded(self, payload, flags):
        if flags & Flags.SYNC and flags & Flags.REQUEST and isinstance(payload, dict):
            sent.append(payload)
        return await original(self, payload, flags)

    monkeypatch.setattr(Protocol, "_call", recorded)
    return sent


def chars(definitions: list[dict[str, Any]]) -> int:
    return sum(len(item["source"]) for definition in definitions for item in definition["sources"].values())


@pytest.mark.asyncio
async def test_classes_of_one_package_share_one_bundle(packages, synced, isolated_remote):
    package, _ = packages
    assert await isolated_remote(package.First.value) == "first:first"
    whole = chars(synced)
    assert whole > 0
    assert await isolated_remote(package.Second.value) == "second:second"
    assert await isolated_remote(package.Third.value) == "third:third"
    # Three announcements, one bundle.
    assert len(synced) == 3
    assert chars(synced) == whole
    assert synced[1]["sources"] == synced[2]["sources"] == {}


@pytest.mark.asyncio
async def test_concurrent_cold_calls_send_the_bundle_once(packages, synced, isolated_remote):
    package, _ = packages
    classes = (package.First, package.Second, package.Third)
    results = await asyncio.gather(*(isolated_remote(item.value) for item in classes))
    assert results == ["first:first", "second:second", "third:third"]
    assert len(synced) == 3
    assert len([item for item in synced if item["sources"]]) == 1


@pytest.mark.asyncio
async def test_another_package_still_sends_its_own_bundle(packages, synced, isolated_remote):
    first, second = packages
    assert await isolated_remote(first.First.value) == "first:first"
    assert await isolated_remote(second.First.value) == "first:first"
    assert len(synced) == 2
    assert set(synced[0]["sources"]) == {"bundle_probe", "bundle_probe.helper"}
    assert set(synced[1]["sources"]) == {"other_probe", "other_probe.helper"}


@pytest.mark.asyncio
async def test_a_new_connection_receives_the_bundle_again(packages, synced, isolated_remote):
    package, _ = packages
    assert await isolated_remote(package.First.value) == "first:first"
    whole = chars(synced)
    synced.clear()
    # Deduplication holds for one connection, because the peer of the next one
    # knows nothing.
    async with await Protocol.from_command(python=sys.executable) as other:
        assert await other(package.Second.value) == "second:second"
    assert chars(synced) == whole


@pytest.mark.asyncio
async def test_a_module_function_shares_the_bundle_with_a_class(packages, synced, isolated_remote):
    package, _ = packages
    helper = importlib.import_module("bundle_probe.helper")
    assert await isolated_remote(package.First.value) == "first:first"
    whole = chars(synced)
    assert await isolated_remote(helper.mark, "plain") == "plain:plain"
    assert len(synced) == 2
    assert synced[1]["kind"] == "module"
    assert chars(synced) == whole
