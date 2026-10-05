from pathlib import Path

import pytest

from rmote.protocol import Protocol
from rmote.templates import Template
from rmote.tools.template import RenderTemplate as TemplateTool


@pytest.mark.asyncio
async def test_module_tool_render_simple(protocol: Protocol) -> None:
    assert await protocol(TemplateTool.render, "Val: {{ v }}", v=7) == "Val: 7"


@pytest.mark.asyncio
async def test_module_tool_render_for_loop(protocol: Protocol) -> None:
    tmpl = "{% for x in items -%}\n- {{ x }}\n{% endfor %}"
    assert await protocol(TemplateTool.render, tmpl, items=["a", "b"]) == "- a\n- b\n"


@pytest.mark.asyncio
async def test_module_tool_render_nested_for(protocol: Protocol) -> None:
    tmpl = "{% for i in rows %}{% for j in cols %}{{ i }},{{ j }}\n{% endfor %}{% endfor %}"
    result = await protocol(TemplateTool.render, tmpl, rows=[1, 2], cols=["x", "y"])
    assert result == "1,x\n1,y\n2,x\n2,y\n"


@pytest.mark.asyncio
async def test_module_tool_render_nested_if(protocol: Protocol) -> None:
    tmpl = "{% if a %}{% if b %}both{% else %}only_a{% endif %}{% else %}none{% endif %}"
    assert await protocol(TemplateTool.render, tmpl, a=True, b=True) == "both"
    assert await protocol(TemplateTool.render, tmpl, a=True, b=False) == "only_a"
    assert await protocol(TemplateTool.render, tmpl, a=False, b=True) == "none"


@pytest.mark.asyncio
async def test_module_tool_render_if_in_for(protocol: Protocol) -> None:
    tmpl = "{% for x in items %}{% if x > 0 %}+{% endif %}{{ x }}\n{% endfor %}"
    result = await protocol(TemplateTool.render, tmpl, items=[1, -2, 3])
    assert result == "+1\n-2\n+3\n"


@pytest.mark.asyncio
async def test_module_tool_render_3level_nested_for(protocol: Protocol) -> None:
    tmpl = "{% for a in [1,2] %}{% for b in ['x','y'] %}{% for c in ['+','-'] %}{{ a }}{{ b }}{{ c }}\n{% endfor %}{% endfor %}{% endfor %}"
    lines = (await protocol(TemplateTool.render, tmpl)).splitlines()
    assert lines[0] == "1x+"
    assert lines[1] == "1x-"
    assert len(lines) == 8


@pytest.mark.asyncio
async def test_module_tool_render_template_object(protocol: Protocol) -> None:
    ct = Template("Hello, {{ name }}!")
    assert await protocol(TemplateTool.render, ct, name="World") == "Hello, World!"


@pytest.mark.asyncio
@pytest.mark.parametrize("as_path", [False, True])
async def test_module_tool_render_file(protocol: Protocol, tmp_path: Path, as_path: bool) -> None:
    tmpl_file = tmp_path / "t.txt"
    tmpl_file.write_text("{% for x in items %}{{ x }}\n{% endfor %}")
    result = await protocol(TemplateTool.render_file, tmpl_file if as_path else str(tmpl_file), items=["p", "q"])
    assert result == "p\nq\n"
