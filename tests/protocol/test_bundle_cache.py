"""A module bundle is built once for a root, not once for every caller.

Building a bundle parses the source of every module it contains. The result
depends only on the root, so it is cached for the life of the process.
"""

import ast
from typing import Any, cast

import pytest

import rmote.templates as templates_module
import rmote.tools.facts as facts_module
from rmote.protocol import BaseProtocol, Flags, ModuleBundle, tool_to_dict
from rmote.tools.facts.collectors import AptFacts, CpuFacts, MemoryFacts


@pytest.fixture
def clean_cache():
    ModuleBundle.bundle.cache_clear()
    yield
    ModuleBundle.bundle.cache_clear()


def test_a_root_is_parsed_once_however_often_it_is_asked_for(clean_cache, monkeypatch):
    parsed = []
    original = ast.parse

    def counted(source, *args, **kwargs):
        parsed.append(len(source))
        return original(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted)
    first = ModuleBundle.build(facts_module)
    after_first = len(parsed)
    assert after_first == len(first)

    for _ in range(50):
        ModuleBundle.build(facts_module)
    assert len(parsed) == after_first


def test_the_bundle_of_a_root_is_stable(clean_cache):
    assert ModuleBundle.build(facts_module) == ModuleBundle.build(facts_module)
    assert ModuleBundle.build(templates_module) == ModuleBundle.build(templates_module)
    assert set(ModuleBundle.build(facts_module)) != set(ModuleBundle.build(templates_module))


def test_every_class_of_a_package_shares_one_bundle(clean_cache):
    definitions = [tool_to_dict(collector) for collector in (AptFacts, CpuFacts, MemoryFacts)]
    assert definitions[0]["sources"] == definitions[1]["sources"] == definitions[2]["sources"]
    assert ModuleBundle.bundle.cache_info().misses == 1


def test_the_caller_cannot_change_the_cached_bundle(clean_cache):
    bundle = ModuleBundle.build(facts_module)
    name = next(iter(bundle))
    bundle.pop(name)
    # The cached mapping is read-only, and the caller works on a copy.
    assert name in ModuleBundle.build(facts_module)
    with pytest.raises(TypeError):
        ModuleBundle.bundle(facts_module)[name] = bundle  # type: ignore[index]


def test_a_class_in_an_argument_reads_the_cache(clean_cache, monkeypatch):
    protocol = BaseProtocol(cast(Any, None), cast(Any, None))
    # The bundle of the package is already built, so serializing a class of it
    # must not walk its sources again.
    ModuleBundle.build(facts_module)
    parsed = []
    original = ast.parse

    def counted(source, *args, **kwargs):
        parsed.append(len(source))
        return original(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted)
    _, _, modules = protocol.serialize(CpuFacts, Flags.RPC)
    assert "rmote.tools.facts" in modules
    assert parsed == []


def test_a_recursive_call_is_not_cached(clean_cache):
    # A partial result depends on what the caller already has, so it must not
    # reach the cache of whole bundles.
    seen = {facts_module.__name__}
    assert ModuleBundle.build(facts_module, seen) == {}
    assert ModuleBundle.bundle.cache_info().misses == 0
    assert ModuleBundle.build(facts_module)
