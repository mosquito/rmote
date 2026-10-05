import copy
import pickle
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

import pytest

from rmote.immutable import DeepMappingProxy, freeze


@dataclass
class Model:
    values: list[int]
    label: str = field(init=False, default="model")


def test_detached_recursive_snapshot_and_readonly_views():
    nested = {"count": 1}
    rows = [nested]
    tags = {"a"}
    model = Model([1])
    source = {"rows": rows, "tags": tags, "model": model, "tuple": (nested,)}
    snapshot: Any = DeepMappingProxy(source)
    source.clear()
    rows.append({"count": 2})
    nested["count"] = 3
    tags.add("b")
    model.values.append(2)
    assert snapshot == {
        "rows": ({"count": 1},),
        "tags": frozenset({"a"}),
        "model": {"values": (1,), "label": "model"},
        "tuple": ({"count": 1},),
    }
    with pytest.raises(TypeError):
        snapshot["new"] = 1
    with pytest.raises(TypeError):
        snapshot["rows"][0]["count"] = 4
    with pytest.raises(AttributeError):
        snapshot["rows"].append(1)
    with pytest.raises(AttributeError):
        snapshot["tags"].add("c")
    with pytest.raises(TypeError):
        snapshot["model"]["values"] = ()
    assert dict(snapshot.items())["rows"] is snapshot["rows"]
    assert list(snapshot.values())[0] is snapshot["rows"]


def test_aliases_and_existing_snapshots_are_reused():
    shared = [{"value": 1}]
    snapshot = freeze({"left": shared, "right": shared})
    assert snapshot["left"] is snapshot["right"]
    assert freeze(snapshot) is snapshot
    assert copy.copy(snapshot) is snapshot
    assert copy.deepcopy(snapshot) is snapshot
    assert freeze({"existing": snapshot})["existing"] is snapshot
    detached = copy.deepcopy({"snapshot": snapshot})
    assert detached["snapshot"] is snapshot


def test_updated_shares_unchanged_branches_and_keeps_old_snapshot():
    snapshot: Any = freeze({"stable": [{"value": 1}], "changed": {"before": True}})
    replacement = {"after": [2]}
    updated = snapshot.updated({"changed": replacement})
    replacement["after"].append(3)
    assert updated["stable"] is snapshot["stable"]
    assert snapshot["changed"] == {"before": True}
    assert updated["changed"] == {"after": (2,)}
    assert snapshot.updated({}) is snapshot
    with pytest.raises(TypeError):
        snapshot.updated({"changed": object()})
    assert snapshot["changed"] == {"before": True}


@pytest.mark.parametrize("kind", ["dict", "list", "dataclass", "indirect"])
def test_cycles_rejected(kind):
    source: Any
    if kind == "dict":
        source = {}
        source["self"] = source
    elif kind == "list":
        source = []
        source.append(source)
    elif kind == "dataclass":
        source = Model([])
        source.values.append(source)
    else:
        source = []
        source.append((source,))
    with pytest.raises(ValueError, match="cyclic"):
        freeze(source)


def test_plain_mapping_proxy_is_detached_too():
    source = {"values": [1]}
    snapshot = freeze(MappingProxyType(source))
    source["values"].append(2)
    assert snapshot["values"] == (1,)


def test_keys_are_preserved_only_when_immutable():
    keys = [None, "key", b"bytes", 4, ("a", 1), frozenset({1, 2}), range(3)]
    snapshot = freeze(dict.fromkeys(keys, "value"))
    assert list(snapshot) == keys

    class MutableKey:
        pass

    with pytest.raises(TypeError, match="mapping key"):
        freeze({MutableKey(): 1})


@pytest.mark.parametrize("value", [object(), bytearray(b"x"), iter([1]), Model])
def test_unknown_mutable_values_are_not_leaked(value):
    with pytest.raises(TypeError, match="Cannot freeze"):
        freeze({"nested": [value]})


def test_scalar_subclasses_are_not_assumed_immutable():
    class MutableInt(int):
        extra: list[int]

    value = MutableInt(1)
    value.extra = []
    with pytest.raises(TypeError):
        freeze(value)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_pickle_preserves_aliases_and_immutable_api(protocol):
    shared = [{"value": 1}]
    original = freeze({"a": shared, "b": shared, "model": Model([2])})
    restored = pickle.loads(pickle.dumps(original, protocol=protocol))
    assert restored == original
    assert restored["a"] is restored["b"]
    with pytest.raises(TypeError):
        restored["a"][0]["value"] = 2
    assert copy.deepcopy(restored) is restored


def test_cannot_reinitialize_or_assign_snapshot_storage():
    snapshot: Any = freeze({"a": 1})
    with pytest.raises(TypeError):
        snapshot.__init__({"a": 2})
    with pytest.raises(TypeError):
        snapshot._DeepMappingProxy__built = False
    with pytest.raises(TypeError):
        del snapshot._DeepMappingProxy__built
    assert snapshot == {"a": 1}
    with pytest.raises(TypeError, match="requires a mapping"):
        DeepMappingProxy(cast(Any, [1]))


def test_mapping_with_temporary_values():
    class Generated(Mapping[int, object]):
        def __iter__(self):
            return iter(range(1000))

        def __len__(self):
            return 1000

        def __getitem__(self, key):
            return {"value": [key]}

    snapshot: Any = freeze(Generated())
    assert [snapshot[index]["value"] for index in range(1000)] == [(i,) for i in range(1000)]


def test_every_method_that_would_change_the_content_refuses():
    snapshot: Any = freeze({"a": 1})
    with pytest.raises(TypeError):
        snapshot.update({"a": 2})
    with pytest.raises(TypeError):
        snapshot.setdefault("b", 2)
    with pytest.raises(TypeError):
        snapshot.pop("a")
    with pytest.raises(TypeError):
        snapshot.popitem()
    with pytest.raises(TypeError):
        snapshot.clear()
    with pytest.raises(TypeError):
        del snapshot["a"]
    with pytest.raises(TypeError):
        snapshot |= {"b": 2}
    with pytest.raises(TypeError, match="not keys"):
        DeepMappingProxy.fromkeys(["a"])  # type: ignore[arg-type]  # the refusal is also static
    assert snapshot == {"a": 1}


def test_a_snapshot_is_a_dictionary_that_keeps_itself_on_copy():
    snapshot: Any = freeze({"a": 1})
    assert isinstance(snapshot, dict)
    assert type(snapshot) is DeepMappingProxy
    assert snapshot.copy() is snapshot
    # A read-only combine gives a plain dictionary and leaves the snapshot.
    combined = snapshot | {"b": 2}
    assert combined == {"a": 1, "b": 2}
    assert type(combined) is dict
    assert snapshot == {"a": 1}
    assert repr(snapshot) == "DeepMappingProxy({'a': 1})"
