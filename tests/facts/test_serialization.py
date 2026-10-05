"""Class identity and raw data survive both persistence representations."""

import json
from dataclasses import dataclass, field

import pytest

from rmote.cache import CacheEntry
from rmote.serialization import dump_dataclass, load_dataclass
from rmote.tools.facts.collectors import NetworkFacts
from rmote.tools.facts.collectors.network import NetworkInfo
from tests.facts.collectors.test_network_schema import linux
from tests.facts.tools import Payload


@dataclass(frozen=True, slots=True)
class Record:
    value: int
    computed: int = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "computed", self.value * 2)


def test_nested_models_and_reserved_raw_dictionaries():
    raw = {"$dataclass": "no.such:Class", "fields": {"x": 1}, "$dict": {"$dict": {}}}
    value = Payload({"raw": raw, "models": [Record(3), NetworkFacts.normalize(linux())]})
    encoded = dump_dataclass(value)
    restored = load_dataclass(json.loads(json.dumps(encoded)), Payload)
    assert restored == value
    assert type(restored.values["models"][0]) is Record
    assert type(restored.values["models"][1]) is NetworkInfo
    assert type(restored.values["models"][1].default.ipv4).__name__ == "DefaultRoute"
    assert restored.values["raw"] == raw
    assert "computed" not in encoded["_value"]["fields"]["values"]["models"][0]["_value"]["fields"]


@pytest.mark.asyncio
async def test_backends_restore_nested_classes(cache):
    network = NetworkFacts.normalize(linux())
    entry = CacheEntry(network, 10, NetworkFacts.version)
    await cache.set("host", "network", entry)
    restored = await type(cache)(cache.path).get("host", "network")
    assert restored == entry
    assert type(restored.data) is NetworkInfo
    assert type(restored.data.ipv4) is type(network.ipv4)
    assert restored.data.ipv4.routes is not None
    assert network.ipv4.routes is not None
    assert type(restored.data.ipv4.routes[0]) is type(network.ipv4.routes[0])
    assert type(restored.data.default.ipv4) is type(network.default.ipv4)


def test_invalid_and_local_classes():
    @dataclass
    class Local:
        value: int

    with pytest.raises(ValueError, match="importable"):
        dump_dataclass(Local(1))
    with pytest.raises(ValueError, match="Not a dataclass"):
        load_dataclass({"_type": "dataclass", "_value": {"class": "builtins:dict", "fields": {}}})
    with pytest.raises(ValueError, match="Expected NetworkInfo"):
        load_dataclass(dump_dataclass(Record(1)), NetworkInfo)
    with pytest.raises(ValueError, match="Invalid fields"):
        load_dataclass(
            {"_type": "dataclass", "_value": {"class": f"{Record.__module__}:Record", "fields": {"unknown": 1}}}
        )
    cyclic = Payload({})
    cyclic.values["self"] = cyclic
    with pytest.raises(ValueError, match="Circular reference"):
        dump_dataclass(cyclic)
