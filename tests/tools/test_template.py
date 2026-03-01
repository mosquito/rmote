from pathlib import Path

import pytest

from rmote.protocol import Protocol, Template
from rmote.tools.template import Template as TemplateTool


@pytest.mark.asyncio
async def test_module_tool_render_simple(protocol: Protocol) -> None:
    assert await protocol(TemplateTool.render, "Val: ${v}", v=7) == "Val: 7"


@pytest.mark.asyncio
async def test_module_tool_render_for_loop(protocol: Protocol) -> None:
    tmpl = "% for x in items:\n- ${x}\n% endfor"
    assert await protocol(TemplateTool.render, tmpl, items=["a", "b"]) == "- a\n- b"


@pytest.mark.asyncio
async def test_module_tool_render_nested_for(protocol: Protocol) -> None:
    tmpl = "% for i in rows:\n% for j in cols:\n${i},${j}\n% endfor\n% endfor"
    result = await protocol(TemplateTool.render, tmpl, rows=[1, 2], cols=["x", "y"])
    assert result == "1,x\n1,y\n2,x\n2,y"


@pytest.mark.asyncio
async def test_module_tool_render_nested_if(protocol: Protocol) -> None:
    tmpl = "% if a:\n% if b:\nboth\n% else:\nonly_a\n% endif\n% else:\nnone\n% endif"
    assert await protocol(TemplateTool.render, tmpl, a=True, b=True) == "both"
    assert await protocol(TemplateTool.render, tmpl, a=True, b=False) == "only_a"
    assert await protocol(TemplateTool.render, tmpl, a=False, b=True) == "none"


@pytest.mark.asyncio
async def test_module_tool_render_if_in_for(protocol: Protocol) -> None:
    tmpl = "% for x in items:\n% if x > 0:\n+${x}\n% else:\n${x}\n% endif\n% endfor"
    result = await protocol(TemplateTool.render, tmpl, items=[1, -2, 3])
    assert result == "+1\n-2\n+3"


@pytest.mark.asyncio
async def test_module_tool_render_3level_nested_for(protocol: Protocol) -> None:
    tmpl = (
        "% for a in [1,2]:\n% for b in ['x','y']:\n% for c in ['+','-']:\n${a}${b}${c}\n% endfor\n% endfor\n% endfor"
    )
    lines = (await protocol(TemplateTool.render, tmpl)).splitlines()
    assert lines[0] == "1x+"
    assert lines[1] == "1x-"
    assert len(lines) == 8


@pytest.mark.asyncio
async def test_module_tool_render_compiled(protocol: Protocol) -> None:
    ct = Template("Hello, ${name}!")
    assert await protocol(TemplateTool.render_compiled, ct, name="World") == "Hello, World!"


@pytest.mark.asyncio
async def test_module_tool_render_file(protocol: Protocol, tmp_path: Path) -> None:
    tmpl_file = tmp_path / "t.txt"
    tmpl_file.write_text("% for x in items:\n${x}\n% endfor")
    result = await protocol(TemplateTool.render_file, str(tmpl_file), items=["p", "q"])
    assert result == "p\nq"
