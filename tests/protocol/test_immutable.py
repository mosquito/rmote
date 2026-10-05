"""Immutable snapshots carry their implementation through the normal loader."""

import copy
from typing import Any

import pytest

from rmote.immutable import DeepMappingProxy, freeze
from rmote.protocol import Tool


@pytest.mark.asyncio
async def test_snapshot_transfers_to_isolated_remote(isolated_remote):
    class InspectSnapshot(Tool):
        @staticmethod
        def inspect(snapshot):
            import copy

            assert isinstance(snapshot, DeepMappingProxy)
            assert copy.deepcopy(snapshot) is snapshot
            data: Any = snapshot
            try:
                data["nested"][0]["value"] = 9
            except TypeError:
                return snapshot.updated({"remote": True})
            raise AssertionError("snapshot is mutable")

    source = {"nested": [{"value": 1}]}
    snapshot = freeze(source)
    source["nested"][0]["value"] = 2
    restored = await isolated_remote(InspectSnapshot.inspect, snapshot)
    assert restored == {"nested": ({"value": 1},), "remote": True}
    assert "remote" not in snapshot
    assert copy.deepcopy(restored) is restored
    with pytest.raises(TypeError):
        restored["remote"] = False


@pytest.mark.asyncio
async def test_freeze_module_tool_returns_snapshot(isolated_remote):
    restored = await isolated_remote(freeze, {"values": [1, 2]})
    assert isinstance(restored, DeepMappingProxy)
    assert restored == {"values": (1, 2)}
