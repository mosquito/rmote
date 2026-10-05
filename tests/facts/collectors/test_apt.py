import shutil
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from rmote.tools.facts.collectors import AptFacts, apt


def test_installed_packages_preserve_architectures_and_holds():
    packages = AptFacts.packages(
        "libdemo:amd64\t1:2.0-3\tamd64\tinstall ok installed\n"
        "libdemo:i386\t1:2.0-3\ti386\thold ok installed\n"
        "removed\t1\tall\tdeinstall ok config-files\n"
        "broken\t1\tall\tinstall reinstreq half-installed\n"
    )
    assert set(packages) == {"libdemo:amd64", "libdemo:i386"}
    assert asdict(packages["libdemo:i386"]) == {
        "version": "1:2.0-3",
        "architecture": "i386",
        "status": "hold ok installed",
    }
    assert AptFacts.packages("") == {}
    with pytest.raises(ValueError):
        AptFacts.packages("invalid output")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["apt-get", "dpkg-query"])
async def test_missing_required_binary_does_not_spawn(monkeypatch, missing):
    monkeypatch.setattr(shutil, "which", lambda name: None if name == missing else "/usr/bin/" + name)
    execute = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(apt, "async_process", execute)
    assert asdict(await AptFacts.collect()) == {"available": False, "version": None, "packages": None}
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_debian_inventory_over_rpc(docker_protocol):
    result = asdict(await docker_protocol(AptFacts.collect))
    assert result["available"]
    assert result["version"].startswith("apt ")
    packages = result["packages"]
    assert packages["dpkg"]["version"]
    assert packages["dpkg"]["architecture"]
    assert packages["dpkg"]["status"] == "install ok installed"
