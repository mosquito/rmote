import shutil
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from rmote.tools.facts.collectors import PacmanFacts, pacman


def test_package_versions_preserve_epochs_and_releases():
    assert {
        key: asdict(value) for key, value in PacmanFacts.packages("linux 6.12.1.arch1-1\ndemo 2:1.0-4\n").items()
    } == {
        "linux": {"version": "6.12.1.arch1-1"},
        "demo": {"version": "2:1.0-4"},
    }
    assert PacmanFacts.packages("") == {}
    with pytest.raises(ValueError):
        PacmanFacts.packages("invalid")


@pytest.mark.asyncio
async def test_missing_binary_does_not_spawn(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    execute = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(pacman, "async_process", execute)
    assert asdict(await PacmanFacts.collect()) == {"available": False, "version": None, "packages": None}
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_arch_inventory_over_rpc(pacman_docker_protocol):
    result = asdict(await pacman_docker_protocol(PacmanFacts.collect))
    assert result["available"]
    assert "Pacman v" in result["version"]
    assert result["packages"]["pacman"]["version"]
    assert result["packages"]["python"]["version"]
