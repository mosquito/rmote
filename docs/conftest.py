"""Execute client documentation against real subprocess and SSH transports."""

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from markdown_pytest import parse_code_blocks  # type: ignore[import-untyped]


@pytest.fixture
def inventory_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    document = Path(__file__).with_name("writing-tools.md")
    source = "\n".join(
        line
        for block in parse_code_blocks(str(document))
        if block.name == "test_inventory_module"
        for line in block.lines
    )
    assert source, "The documented inventory module must exist"
    path = tmp_path / "inventory.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("inventory", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "inventory", module)
    spec.loader.exec_module(module)
    yield module


@pytest.fixture(name="__name__")
def client_main_name(client_resources: object) -> str:
    # markdown-pytest normally evaluates blocks without __name__, so main guards
    # do not run. These examples import a real Tool module and must execute main.
    return "__main__"


@pytest.fixture
def lifecycle_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    document = Path(__file__).with_name("quickstart.md")
    source = "\n".join(
        line
        for block in parse_code_blocks(str(document))
        if block.name == "test_lifecycle_tool_module"
        for line in block.lines
    )
    assert source, "The documented lifecycle module must exist"
    path = tmp_path / "lifecycle_tools.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("lifecycle_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "lifecycle_tools", module)
    spec.loader.exec_module(module)
    yield module
