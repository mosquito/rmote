"""Remote template rendering through inline tools."""

import pytest

from rmote.protocol import Protocol, Tool
from rmote.templates import Template, render_template


@pytest.mark.asyncio
async def test_templates_in_inline_tools(protocol: Protocol):
    class Renderer(Tool):
        @staticmethod
        def render(source: str, **ctx: object) -> str:
            return render_template(source, **ctx)

        @staticmethod
        def compiled(template: Template, **ctx: object) -> str:
            return template.render(**ctx)

    source = "{#- hosts -#}{% for host in hosts %}{% if host %}[{{ host }}]{% endif %}{% endfor %}\n"
    for hosts, expected in [(["a", "", "b"], "[a][b]\n"), ([], "\n")]:
        assert await protocol(Renderer.render, source, hosts=hosts) == expected
        assert await protocol(Renderer.compiled, Template(source), hosts=hosts) == expected
