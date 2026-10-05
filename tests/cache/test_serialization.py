import json

import pytest

from rmote.cache import CacheEntry
from rmote.serialization import json_default, json_object_hook
from tests.cache.models import Payload


def test_default_is_shallow_and_json_handles_the_tree():
    raw = {"bytes": b"\x00\xff", "nested": [Payload({"answer": 42})]}
    record = Payload(raw)
    encoded = json_default(record)
    assert encoded["_value"]["fields"]["values"] is raw
    assert json_default(b"\x00\xff") == {"_type": "bytes", "_value": "AP8="}
    restored = json.loads(json.dumps(record, default=json_default), object_hook=json_object_hook)
    assert restored == record
    assert type(restored) is Payload
    assert type(restored.values["nested"][0]) is Payload


@pytest.mark.asyncio
async def test_backends_store_ordinary_json_bytes_and_dataclasses(cache):
    values = {"output": b"\x00\xff", "model": Payload({"data": b"hello"}), "scalar": 42, "null": None}
    entries = {key: CacheEntry(value, 1) for key, value in values.items()}
    await cache.set_many("scope", entries)
    restored = await cache.get_many("scope", None)
    assert restored == entries
    assert type(restored["model"].data) is Payload


@pytest.mark.parametrize("value", [set(), object()])
def test_unknown_types_are_json_type_errors(value):
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(value, default=json_default)


@pytest.mark.parametrize(
    "value",
    [
        {"_type": "bytes", "_value": "not base64!!"},
        {"_type": "bytes", "_value": 1},
        {"_type": "unknown", "_value": None},
        {"_type": "dataclass", "_value": {"class": "builtins:dict", "fields": {}}},
    ],
)
def test_invalid_reserved_records_fail(value):
    with pytest.raises(ValueError):
        json.loads(json.dumps(value), object_hook=json_object_hook)


def test_other_dictionary_shapes_are_ordinary_data():
    for value in ({"_type": "bytes"}, {"_value": "raw"}, {"_type": "bytes", "_value": "raw", "extra": True}):
        assert json.loads(json.dumps(value), object_hook=json_object_hook) == value


def test_a_class_is_resolved_once_for_a_whole_document():
    """Encoding and decoding ask about a class once, however often it repeats."""
    from rmote.serialization import dataclass_path, init_fields, resolve_class

    for cache in (resolve_class, dataclass_path, init_fields):
        cache.cache_clear()
    values = [Payload({"index": index}) for index in range(200)]
    text = json.dumps(values, default=json_default, allow_nan=False)
    assert resolve_class.cache_info().misses == 1
    assert dataclass_path.cache_info().misses == init_fields.cache_info().misses == 1
    resolve_class.cache_clear()
    assert json.loads(text, object_hook=json_object_hook) == values
    assert resolve_class.cache_info().misses == 1
    # Repeated paths and classes must not grow the caches without a limit.
    for cache in (resolve_class, dataclass_path, init_fields):
        assert cache.cache_info().maxsize is not None


def test_a_local_dataclass_is_rejected_every_time():
    from dataclasses import dataclass

    from rmote.serialization import resolve_class

    @dataclass
    class Local:
        value: int

    resolve_class.cache_clear()
    for _ in range(3):
        with pytest.raises(ValueError, match="importable module:qualname"):
            json.dumps(Local(1), default=json_default, allow_nan=False)
    # A rejected path is never kept, so every attempt is checked again.
    assert resolve_class.cache_info().currsize == 0


def test_a_class_replaced_at_its_path_is_rejected(tmp_path, monkeypatch):
    import importlib
    import sys
    from dataclasses import dataclass

    from rmote.serialization import dataclass_path, resolve_class

    (tmp_path / "identity_probe.py").write_text(
        "from dataclasses import dataclass\n\n\n@dataclass\nclass Model:\n    value: int = 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("identity_probe")
    try:
        instance = module.Model(2)

        @dataclass
        class Other:
            value: int = 1

        Other.__qualname__ = "Model"
        monkeypatch.setattr(module, "Model", Other)
        resolve_class.cache_clear()
        dataclass_path.cache_clear()
        with pytest.raises(ValueError, match="identity changed"):
            json.dumps(instance, default=json_default, allow_nan=False)
    finally:
        del sys.modules["identity_probe"]
        resolve_class.cache_clear()
        dataclass_path.cache_clear()
