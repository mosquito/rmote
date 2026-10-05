"""Filter classes and template mappings through an autonomous remote bootstrap."""

import asyncio
import sys
from pathlib import Path

import pytest

from rmote.protocol import Protocol
from rmote.templates import Template
from rmote.templates.filters import TEMPLATE_FILTERS
from rmote.tools.template import RenderTemplate as RemoteTemplate
from tests.support.tool_cases.filter_tool import FilterTemplateTool, HostFilter


@pytest.mark.asyncio
async def test_filters_over_rpc(protocol: Protocol):
    source = "{% set network = address|ipaddress('network') %}{{ network }} {{ names|unique|sort|join(',') }} {{ missing|default('ok') }}"
    context = {"address": "2001:db8::123/64", "names": ["b", "A", "B"]}
    expected = "2001:db8::/64 A,b ok"
    assert await protocol(RemoteTemplate.render, source, **context) == expected
    assert await protocol(RemoteTemplate.render, Template(source), **context) == expected


@pytest.mark.asyncio
async def test_custom_filters_transferred_to_remote(protocol: Protocol):
    from rmote.protocol import Tool

    class LocalFilter(HostFilter):
        prefix = "local:"

    class FilterConsumer(Tool):
        @staticmethod
        def apply(cls, value):
            return cls()(value)

        @staticmethod
        def echo(value):
            return value

    template = Template(
        "{{ name|custom('!') }} {{ address|ipaddress('network') }}",
        filters={"custom": LocalFilter, "ipaddress": TEMPLATE_FILTERS["ipaddress"]},
    )
    assert (
        await protocol(RemoteTemplate.render, template, name="Web Server", address="192.0.2.7/24")
        == "local:web-server! 192.0.2.0/24"
    )
    assert await protocol(FilterConsumer.apply, LocalFilter, "DB Server") == "local:db-server"
    restored_class = await protocol(FilterConsumer.echo, LocalFilter)
    restored_instance = await protocol(FilterConsumer.echo, LocalFilter())
    assert restored_class()("DB Server") == "local:db-server"
    assert restored_instance("DB Server") == "local:db-server"


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_template_with_filters_in_isolated_remote(tmp_path):
    class LocalFilter(HostFilter):
        prefix = "remote:"

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        async with await Protocol.from_subprocess(proc) as remote:
            assert await remote(FilterTemplateTool.package_info) == (
                "rmote.templates",
                "rmote.templates",
                "rmote.templates",
                "rmote.templates.engine",
            )
            assert await remote(FilterTemplateTool.render, "Web Server") == "HOST:WEB-SERVER"
            template = Template(
                "{% set network = address|ipaddress('network') %}{{ network }} {{ name|custom('!')|upper }}",
                filters={**TEMPLATE_FILTERS, "custom": LocalFilter},
            )
            assert (
                await remote(RemoteTemplate.render, template, address="2001:db8::123/64", name="Web Server")
                == "2001:db8::/64 REMOTE:WEB-SERVER!"
            )
            restricted = Template("{{ name|custom }}", filters={"custom": LocalFilter})
            assert await remote(RemoteTemplate.render, restricted, name="DB Server") == "remote:db-server"
            default = Template("{{ address|ipaddress }}")
            assert await remote(RemoteTemplate.render, default, address="192.0.2.7/24") == "192.0.2.7/24"
            attributes = Template("{{ (address|ipaddress).network.prefixlen }}")
            assert await remote(RemoteTemplate.render, attributes, address="192.0.2.7/24") == "24"
            payload = (Path(__file__).parents[1] / "templates/data/legacy_template.pickle").read_bytes()
            restored = await remote(FilterTemplateTool.restore, payload)
            assert isinstance(restored, Template)
            assert restored.render(name="local") == "LOCAL"
            assert await remote(RemoteTemplate.render, restored, name="remote") == "REMOTE"
    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()


@pytest.mark.asyncio
async def test_remote_receives_program_without_recompiling(protocol: Protocol):
    from rmote.protocol import Tool

    class DisableCompiler(Tool):
        @staticmethod
        def disable():
            from typing import Any, cast

            from rmote.templates.engine import Template, TemplateLexer

            def forbidden(*args, **kwargs):
                raise AssertionError("Remote must invoke the transferred IR")

            cast(Any, Template).compile = staticmethod(forbidden)
            cast(Any, TemplateLexer).tokenize = forbidden

    template = Template(
        "{{ name|custom }} {{ address|ipaddress('network') }}", filters={**TEMPLATE_FILTERS, "custom": HostFilter}
    )
    await protocol(DisableCompiler.disable)
    assert (
        await protocol(RemoteTemplate.render, template, name="Web Server", address="192.0.2.7/24")
        == "host:web-server 192.0.2.0/24"
    )


@pytest.mark.asyncio
async def test_iterator_filter_results_over_rpc(protocol: Protocol):
    from rmote.templates.filters import TemplateFilter

    class IteratorsFilter(TemplateFilter):
        def __call__(self, value):
            return {"map": map(str, value), "zip": zip(value, value, strict=True)}

    template = Template("{{ values|iterators|tojson }}", filters={**TEMPLATE_FILTERS, "iterators": IteratorsFilter})
    assert await protocol(RemoteTemplate.render, template, values=[1, 2]) == (
        '{"map": ["1", "2"], "zip": [[1, 1], [2, 2]]}'
    )


@pytest.mark.asyncio
async def test_trusted_filter_objects_over_rpc(protocol: Protocol):
    from rmote.templates.filters import TemplateFilter

    class InterfaceFilter(TemplateFilter):
        def __call__(self, value):
            from ipaddress import ip_interface

            return ip_interface(value)

    template = Template(
        "{% set interface = address|interface %}"
        "{{ interface.ip }} {{ interface.network.network_address }} {{ interface.network.prefixlen }}",
        filters={"interface": InterfaceFilter},
    )
    assert await protocol(RemoteTemplate.render, template, address="192.0.2.7/24") == "192.0.2.7 192.0.2.0 24"
