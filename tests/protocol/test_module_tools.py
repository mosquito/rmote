"""Module tools must work in interpreters without their local packages."""

import asyncio
import importlib
import sys
from dataclasses import is_dataclass
from types import MappingProxyType
from unittest.mock import patch

import pytest

from rmote.protocol import ModuleBundle, tool_key, tool_to_dict


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "module_tool_probe"
    root.mkdir()
    (root / "__init__.py").write_text("""from .models import Record
from .nested import helper
loads = 1

def gather(value: int = 42) -> Record:
    return Record(helper.double(value), loads)

async def async_gather(value: int) -> Record:
    return gather(value)

async def stream(count: int):
    for i in range(count):
        yield gather(i)

def lazy():
    from . import broken
    return broken
""")
    (root / "models.py").write_text("""from __future__ import annotations
from dataclasses import dataclass
@dataclass
class Record:
    value: int
    loads: int
""")
    (root / "nested").mkdir()
    (root / "nested" / "__init__.py").write_text("")
    (root / "nested" / "helper.py").write_text("""from module_tool_probe.models import Record
__tool_package__ = "module_tool_probe"
def double(value):
    return value * 2
""")
    (root / "broken.py").write_text('raise RuntimeError("broken import")\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("module_tool_probe")
    yield module
    for name in list(sys.modules):
        if name == module.__name__ or name.startswith(module.__name__ + "."):
            del sys.modules[name]


@pytest.mark.asyncio
async def test_package_imports_dataclasses_streams_and_concurrent_load(package, isolated_remote):
    remote = isolated_remote
    results = await asyncio.gather(*(remote(package.async_gather, i) for i in range(8)))
    assert [item.value for item in results] == list(range(0, 16, 2))
    assert all(is_dataclass(item) and type(item) is package.Record and item.loads == 1 for item in results)
    assert (await remote(package.gather)).value == 84
    assert [item.value async for item in remote.stream(package.stream, 3)] == [0, 2, 4]
    helper = importlib.import_module("module_tool_probe.nested.helper")
    assert await remote(helper.double, 5) == 10
    # A failed lazy import must not break the protocol or poison the package.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="broken import"):
            await remote(package.lazy)
    assert (await remote(package.gather, 1)).value == 2


@pytest.mark.asyncio
async def test_single_module_and_cached_bundle(package, isolated_remote, tmp_path, monkeypatch):
    (tmp_path / "single_probe.py").write_text("def run(value):\n    return value + 1\n")
    module = importlib.import_module("single_probe")
    try:
        assert await isolated_remote(module.run, 4) == 5
    finally:
        sys.modules.pop("single_probe", None)
    await isolated_remote(package.gather)
    helper = importlib.import_module("module_tool_probe.nested.helper")
    with open(str(helper.__file__), "a") as stream:
        stream.write("\n# source changed\n")
    assert await isolated_remote(helper.double, 5) == 10


def test_bundle_sources_reused_across_calls(package):
    helper = importlib.import_module("module_tool_probe.nested.helper")
    with patch.object(ModuleBundle, "source", wraps=ModuleBundle.source) as source:
        expected = ModuleBundle.build(package)
        reads = source.call_count
        assert reads == len(expected)
        for _ in range(100):
            assert ModuleBundle.build(package) == expected
            assert ModuleBundle.build(helper) == expected
        assert source.call_count == reads


def test_dependencies_do_not_modify_cached_sources(package, monkeypatch):
    models = importlib.import_module("module_tool_probe.models")
    monkeypatch.setattr(models, "__tool_dependencies__", (package.__name__,), raising=False)
    monkeypatch.setattr(package, "__tool_dependencies__", (models.__name__,), raising=False)
    sources = ModuleBundle.build(models)
    assert "module_tool_probe.nested.helper" in sources
    assert isinstance(ModuleBundle.collect(models), MappingProxyType)
    assert set(ModuleBundle.collect(models)) == {models.__name__}
    sources.clear()
    assert ModuleBundle.build(models)


def test_bundle_explicit_boundary(package):
    helper = importlib.import_module("module_tool_probe.nested.helper")
    sources = ModuleBundle.build(helper)
    assert set(sources) == {
        "module_tool_probe",
        "module_tool_probe.models",
        "module_tool_probe.broken",
        "module_tool_probe.nested",
        "module_tool_probe.nested.helper",
    }
    assert sources["module_tool_probe"]["package"]
    assert not sources["module_tool_probe.models"]["package"]


@pytest.mark.asyncio
async def test_circular_imports_and_simultaneous_module_calls(package, isolated_remote):
    from pathlib import Path

    root = Path(package.__file__).parent
    (root / "left.py").write_text("""__tool_package__ = "module_tool_probe"
from . import right
value = 20
def read():
    return value + right.value
""")
    (root / "right.py").write_text("""__tool_package__ = "module_tool_probe"
from . import left
value = 22
def read():
    return value + left.value
""")
    left = importlib.import_module("module_tool_probe.left")
    right = importlib.import_module("module_tool_probe.right")
    assert list(await asyncio.gather(isolated_remote(left.read), isolated_remote(right.read))) == [42, 42]


@pytest.mark.asyncio
async def test_failed_root_import_can_retry(package, isolated_remote):
    from pathlib import Path

    root = Path(package.__file__).parent
    (root / "bad_left.py").write_text("from .bad_right import value\nvalue = 1\n")
    (root / "bad_right.py").write_text("from .bad_left import value\nvalue = 2\n")
    with open(package.__file__, "a") as stream:
        stream.write("\nfrom .bad_left import value\n")
    for _ in range(2):
        with pytest.raises(ImportError, match="partially initialized module"):
            await isolated_remote(package.gather)
    assert package not in isolated_remote._tools_cache


@pytest.mark.asyncio
async def test_tool_class_argument_and_module_share_one_definition(tmp_path, monkeypatch, isolated_remote):
    (tmp_path / "class_module_probe.py").write_text("""from dataclasses import dataclass
from rmote.protocol import Tool

@dataclass
class Record:
    value: int

class Example(Tool):
    @staticmethod
    def value() -> Record:
        return Record(42)

def invoke(collector):
    return collector.value()
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("class_module_probe")
    try:
        assert await isolated_remote(module.Example.value) == module.Record(42)
        assert await isolated_remote(module.invoke, module.Example) == module.Record(42)
        assert await isolated_remote(module.Example.value) == module.Record(42)
    finally:
        sys.modules.pop("class_module_probe", None)


@pytest.mark.asyncio
async def test_class_and_function_share_declared_package_import_locks(package, isolated_remote):
    from pathlib import Path

    root = Path(package.__file__).parent
    (root / "mixed.py").write_text("""__tool_package__ = "module_tool_probe"
from rmote.protocol import Tool
from .models import Record
class Mixed(Tool):
    @staticmethod
    def run():
        return Record(42, 1)

def run():
    return Mixed.run()
""")
    module = importlib.import_module("module_tool_probe.mixed")
    results = await asyncio.gather(isolated_remote(module.run), isolated_remote(module.Mixed.run))
    assert all(result == package.Record(42, 1) for result in results)


@pytest.mark.asyncio
async def test_class_tool_preserves_imports_and_uses_cached_bundle(tmp_path, monkeypatch, isolated_remote):
    (tmp_path / "facts_class_probe.py").write_text("""from rmote.protocol import Tool
from rmote.tools.facts.collectors import PythonFacts
from rmote.tools.fs import FileSystem

class Probe(Tool):
    @staticmethod
    def gather():
        return PythonFacts.collect().version

    @staticmethod
    def exists(path):
        return FileSystem.stat(path).exists
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("facts_class_probe")
    try:
        with patch.object(ModuleBundle, "source", wraps=ModuleBundle.source) as read:
            definition = tool_to_dict(module.Probe)
            reads = read.call_count
            assert reads > 0
            for _ in range(10):
                assert tool_to_dict(module.Probe) == definition
            assert read.call_count == reads
        assert (
            "from rmote.tools.facts.collectors import PythonFacts" in definition["sources"][module.__name__]["source"]
        )
        assert await isolated_remote(module.Probe.gather)
        assert await isolated_remote(module.Probe.exists, str(tmp_path))
    finally:
        sys.modules.pop(module.__name__, None)


def test_the_key_of_a_definition_is_the_one_the_peer_registers():
    """One function gives the key, so the two sides cannot disagree."""
    assert tool_key({"name": "Items", "qualname": "Items", "module": "pkg.mod", "sources": {}}) == "pkg.mod.Items"
    assert tool_key({"name": "Inner", "qualname": "Outer.Inner", "module": "pkg.mod"}) == "pkg.mod.Outer.Inner"
    assert tool_key({"kind": "module", "name": "pkg.mod", "module": "pkg.mod"}) == "pkg.mod"
    # An inline definition carries source and no module, so its name is the key.
    assert tool_key({"name": "Inline", "source": "class Inline(Tool): pass"}) == "Inline"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_tool_at_the_top_level_of_a_script_is_callable(tmp_path):
    """A class of __main__ has no module the peer can import, so it goes inline.

    The test runs a real script, because only a script has __main__ at its top
    level. The key of such a class is its own name on both sides.
    """
    script = tmp_path / "script_tool.py"
    script.write_text(
        "import asyncio\n"
        "import sys\n"
        "\n"
        "from rmote.protocol import Protocol, Tool\n"
        "\n"
        "\n"
        "class ScriptTool(Tool):\n"
        "    @staticmethod\n"
        "    def ping() -> int:\n"
        "        return 42\n"
        "\n"
        "\n"
        "async def main() -> None:\n"
        "    async with await Protocol.from_command(python=sys.executable) as remote:\n"
        "        print(await remote(ScriptTool.ping))\n"
        "\n"
        "\n"
        "asyncio.run(main())\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()
    assert stdout.strip() == b"42"
