"""Bootstrap is transport-only; object dependencies arrive before unpickling."""

import asyncio
import importlib
import sys

import pytest

from rmote.process import async_process
from rmote.protocol import Flags, Tool
from rmote.templates import Template
from rmote.tools import facts
from rmote.tools.template import RenderTemplate


@pytest.fixture
def lazy_probe(tmp_path, monkeypatch):
    path = tmp_path / "lazy_probe.py"
    path.write_text("""import sys
from rmote.protocol import Tool

def modules():
    return sorted(name for name in sys.modules if name.startswith("rmote.templates"))

def protocol_identity():
    import rmote
    module = sys.modules["rmote.protocol"]
    return rmote.protocol is module and module.run.__globals__ is vars(module)

def process_loaded():
    return "rmote.process" in sys.modules

def echo(value):
    return value

def source_roundtrip(value):
    sys.modules[value.__module__].__file__ = "<remote source>"
    return value

async def items(value):
    yield value

class Echo(Tool):
    @staticmethod
    def echo(value):
        return value
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("lazy_probe")
    yield module
    sys.modules.pop("lazy_probe", None)


@pytest.mark.asyncio
async def test_bootstrap_and_facts_do_not_load_templates(lazy_probe, isolated_remote):
    assert await isolated_remote(lazy_probe.protocol_identity)
    assert await isolated_remote(lazy_probe.modules) == []
    assert (await facts.fetch(isolated_remote, sections=["python"]))["python"].version
    assert await isolated_remote(lazy_probe.modules) == []
    assert await isolated_remote(RenderTemplate.render, "{{ value|upper }}", value="web") == "WEB"
    assert "rmote.templates.engine" in await isolated_remote(lazy_probe.modules)


@pytest.mark.asyncio
async def test_inline_process_dependency_is_loaded_on_demand(lazy_probe, isolated_remote):
    class Command(Tool):
        @staticmethod
        async def run():
            return await async_process("echo", "ready", capture_output=True, text=True)

    assert not await isolated_remote(lazy_probe.process_loaded)
    assert (await isolated_remote(Command.run)).stdout == "ready\n"
    assert await isolated_remote(lazy_probe.process_loaded)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["function", "class", "stream"])
async def test_template_argument_carries_code_once_before_pickle(lazy_probe, isolated_remote, monkeypatch, kind):
    assert await isolated_remote(lazy_probe.modules) == []
    sent = []
    original = isolated_remote.send_payload

    async def capture(payload, flags, packet_id):
        sent.append(flags)
        await original(payload, flags, packet_id)

    monkeypatch.setattr(isolated_remote, "send_payload", capture)
    template = Template("{{ name|upper }}")
    if kind == "stream":
        returned = [value async for value in isolated_remote(lazy_probe.items, template)]
        restored = returned[0]
    else:
        method = lazy_probe.echo if kind == "function" else lazy_probe.Echo.echo
        restored = await isolated_remote(method, {"nested": [template]})
        restored = restored["nested"][0]
    assert restored.render(name="web") == "WEB"
    assert type(restored) is Template
    assert sum(bool(flags & Flags.MODULES) for flags in sent) == 1
    sent.clear()
    again = await isolated_remote(lazy_probe.echo, template)
    assert again.render(name="db") == "DB"
    assert not any(flags & Flags.MODULES for flags in sent)


@pytest.mark.asyncio
async def test_concurrent_first_template_arguments(lazy_probe, isolated_remote):
    results = await asyncio.gather(*(isolated_remote(lazy_probe.echo, Template(str(i))) for i in range(8)))
    assert [item.render() for item in results] == [str(i) for i in range(8)]


@pytest.mark.asyncio
async def test_module_declares_lazy_external_dependency(lazy_probe, isolated_remote, tmp_path):
    (tmp_path / "dependent_probe.py").write_text("""__tool_dependencies__ = ("rmote.templates",)
from rmote.templates import Template

def render(value):
    return Template("{{ value|upper }}").render(value=value)
""")
    module = importlib.import_module("dependent_probe")
    try:
        assert await isolated_remote(lazy_probe.modules) == []
        assert await isolated_remote(module.render, "web") == "WEB"
    finally:
        sys.modules.pop("dependent_probe", None)


@pytest.mark.asyncio
async def test_tool_class_roundtrip_uses_transferred_source(lazy_probe, isolated_remote):
    returned = await isolated_remote(lazy_probe.source_roundtrip, lazy_probe.Echo)
    assert returned is lazy_probe.Echo
