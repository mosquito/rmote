"""A Tool class in an argument carries its sources beside the payload.

The sources of a class travel in the MODULES envelope of the packet, which the
peer installs before it decodes the payload. The payload itself holds only the
identity of the class, so a class the peer already has costs almost nothing.
"""

import importlib
import pickle
import re
import sys

import pytest

from rmote.protocol import BaseProtocol, Flags, Protocol, Tool


@pytest.fixture
def packages(tmp_path, monkeypatch):
    """A package of Tool classes, and another one that runs them."""
    carrier = tmp_path / "argument_probe"
    carrier.mkdir()
    (carrier / "__init__.py").write_text("""__tool_package__ = "argument_probe"

from rmote.protocol import Tool


class Counter(Tool):
    step = 3

    def total(self, times: int) -> int:
        return self.step * times
""")
    runner = tmp_path / "runner_probe"
    runner.mkdir()
    (runner / "__init__.py").write_text("""__tool_package__ = "runner_probe"


def run(counter, times: int) -> int:
    return counter().total(times)


def run_many(items, times: int) -> list:
    return [run(item, times) for item in items]
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    first = importlib.import_module("argument_probe")
    second = importlib.import_module("runner_probe")
    yield first, second
    for module in list(sys.modules):
        if module.startswith(("argument_probe", "runner_probe")):
            del sys.modules[module]


@pytest.fixture
def carried(monkeypatch):
    """Record the module names each packet carries outside its payload."""
    sent: list[set[str]] = []
    payloads: list[bytes] = []
    original = BaseProtocol.serialize

    def recorded(self, value, flags):
        payload, result_flags, modules = original(self, value, flags)
        if flags & Flags.RPC:
            sent.append(modules)
            payloads.append(payload)
        return payload, result_flags, modules

    monkeypatch.setattr(BaseProtocol, "serialize", recorded)
    return sent, payloads


@pytest.mark.asyncio
async def test_a_class_argument_brings_its_package(packages, carried, isolated_remote):
    counter, runner = packages
    sent, payloads = carried
    # The peer syncs runner_probe as a module tool. argument_probe reaches it
    # only as an argument.
    assert await isolated_remote(runner.run, counter.Counter, 4) == 12
    assert sent[0] == {"argument_probe"}
    assert b"__tool_package__" in payloads[0]
    # The second call finds the package known and sends the identity alone.
    assert await isolated_remote(runner.run, counter.Counter, 5) == 15
    assert sent[1] == set()
    assert b"__tool_package__" not in payloads[1]
    assert b"argument_probe" in payloads[1]
    assert len(payloads[1]) < 300


@pytest.mark.asyncio
async def test_classes_inside_containers_arrive_once(packages, carried, isolated_remote):
    counter, runner = packages
    sent, _ = carried
    classes = [counter.Counter, counter.Counter]
    assert await isolated_remote(runner.run_many, classes, 2) == [6, 6]
    assert sent[0] == {"argument_probe"}
    assert await isolated_remote(runner.run_many, (counter.Counter,), 2) == [6]
    assert sent[1] == set()


@pytest.mark.asyncio
async def test_an_inline_class_keeps_its_source_in_the_payload(carried, isolated_remote):
    class Inline(Tool):
        def answer(self) -> int:
            return 42

    sent, _ = carried
    # An inline class has no module to import, so its source stays in the
    # payload and the envelope carries nothing.
    assert await isolated_remote(Inline.answer) == 42
    pickled = BaseProtocol(None, None).serialize(Inline, Flags.RPC)  # type: ignore[arg-type]
    assert pickled[2] == set()
    assert b"def answer" in pickled[0]


def test_a_plain_pickle_of_a_class_stays_self_contained(packages):
    counter, _ = packages
    # Outside the protocol there is no envelope, so copyreg keeps the sources
    # in the pickle itself.
    payload = pickle.dumps(counter.Counter)
    assert b"__tool_package__" in payload
    assert pickle.loads(payload) is counter.Counter


def test_other_copyreg_types_still_reduce():
    # The pickler replaces the table of copyreg, so what copyreg registers
    # must keep working.
    protocol = BaseProtocol(None, None)  # type: ignore[arg-type]
    payload, _, modules = protocol.serialize(re.compile(r"a+b"), Flags.RPC)
    assert modules == set()
    assert pickle.loads(payload).pattern == "a+b"


@pytest.mark.asyncio
async def test_a_class_argument_works_without_a_prior_sync(packages):
    counter, runner = packages
    # A fresh connection has nothing, and the first call carries both the tool
    # and the class of the argument.
    async with await Protocol.from_command(python=sys.executable) as remote:
        assert await remote(runner.run, counter.Counter, 7) == 21
