"""A builtin value must not pay for the search of a transferable module.

Pickle asks `persistent_id` about every object of a payload, so the answer for
builtin types is decided by the exact type alone. A subclass declared in a
transferable module is not a builtin type, and still carries its sources.
"""

import importlib
import io
import pickle
import sys
from typing import Any, cast

import pytest

from rmote.protocol import PLAIN_TYPES, BaseProtocol, Flags, ModuleBundle, ModulePickler


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A transferable package declaring subclasses of builtin types."""
    root = tmp_path / "subclass_probe"
    root.mkdir()
    (root / "__init__.py").write_text("""__tool_package__ = "subclass_probe"


class Settings(dict):
    def names(self):
        return sorted(self)


class Names(list):
    def joined(self):
        return ",".join(self)


class Plain:
    def __init__(self, value=1):
        self.value = value


def echo(value):
    return value
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("subclass_probe")
    yield module
    for name in list(sys.modules):
        if name == module.__name__ or name.startswith(module.__name__ + "."):
            del sys.modules[name]
    ModuleBundle.bundle.cache_clear()
    ModuleBundle.owner_module.cache_clear()


def carried(value: Any) -> set[str]:
    """Return the module names that serializing *value* would transfer."""
    protocol = BaseProtocol(cast(Any, None), cast(Any, None))
    _, flags, names = protocol.serialize(value, Flags.RPC)
    assert bool(flags & Flags.MODULES) is bool(names)
    return names


def test_the_fast_path_tests_the_exact_type():
    # Only exact builtin types take the shortcut, and every one of them is
    # a type that cannot carry a declaration.
    assert all(isinstance(item, type) and item.__module__ == "builtins" for item in PLAIN_TYPES)
    assert {int, str, bytes, list, dict, tuple, type(None)} <= PLAIN_TYPES


def test_builtin_values_carry_nothing():
    assert carried([1, 2, 3]) == set()
    assert carried({"a": 1, "b": (2.5, None, b"x", True)}) == set()
    assert carried({frozenset({1}), "text"}) == set()


def test_a_dict_subclass_still_carries_its_sources(package):
    settings = package.Settings(a=1, b=2)
    assert carried(settings) == {"subclass_probe"}


def test_a_list_subclass_still_carries_its_sources(package):
    assert carried(package.Names(["a", "b"])) == {"subclass_probe"}


@pytest.mark.parametrize(
    "wrap", [lambda item: [item], lambda item: (item,), lambda item: {"key": item}, lambda item: [[item]]]
)
def test_a_subclass_is_found_inside_a_builtin_container(package, wrap):
    # The shortcut answers for the container, and pickle asks again for every
    # element, so nesting does not hide the subclass.
    assert carried(wrap(package.Settings(a=1))) == {"subclass_probe"}


def test_the_shortcut_refuses_a_subclass_of_a_builtin_type(package):
    # Pickling an instance also saves its class, which carries the sources on
    # its own. The decision itself is therefore checked directly: a value whose
    # exact type is not builtin must reach the search.
    pickler = ModulePickler(io.BytesIO(), set())
    pickler.persistent_id(package.Settings(a=1))
    pickler.persistent_id(package.Names(["a"]))
    assert "subclass_probe" in pickler.sources

    untouched = ModulePickler(io.BytesIO(), set())
    for value in ({"a": 1}, [1], (1,), 1, True, "a", b"a", None):
        untouched.persistent_id(value)
    assert untouched.sources == {}


def test_an_ordinary_class_is_unaffected(package):
    assert carried(package.Plain(2)) == {"subclass_probe"}
    assert carried(package.echo) == {"subclass_probe"}
    assert carried(package.Settings) == {"subclass_probe"}


def test_a_subclass_survives_the_roundtrip(package):
    protocol = BaseProtocol(cast(Any, None), cast(Any, None))
    payload, flags, _ = protocol.serialize([package.Settings(b=2, a=1), package.Names(["x"])], Flags.RPC)
    _, inner = pickle.loads(payload)
    settings, names = pickle.loads(inner)
    assert type(settings) is package.Settings
    assert settings.names() == ["a", "b"]
    assert type(names) is package.Names
    assert names.joined() == "x"


@pytest.mark.asyncio
async def test_a_remote_call_returns_a_builtin_subclass(package, isolated_remote):
    settings = await isolated_remote(package.echo, package.Settings(host="::1", port=22))
    assert settings == {"host": "::1", "port": 22}
    assert settings.names() == ["host", "port"]
    names = await isolated_remote(package.echo, package.Names(["a", "b"]))
    assert names.joined() == "a,b"
