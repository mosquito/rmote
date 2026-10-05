import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from rmote.cache import Cache, JSONCacheDir
from rmote.serialization import Dataclass
from rmote.tools import facts
from rmote.tools.exec import Exec
from rmote.tools.facts.collectors import SystemdFacts, SystemdResolvedFacts, SystemdTimesyncFacts
from rmote.tools.facts.collectors.systemd import SystemdCommand


def test_systemd_records_preserve_names_and_fields():
    assert SystemdFacts.unit_records('[{"unit":"foo@bar.service","active":"failed","sub":"failed"}]', "unit") == {
        "foo@bar.service": {"unit": "foo@bar.service", "active": "failed", "sub": "failed"}
    }
    for output in ["{}", '[{"active":"active"}]', "[null]"]:
        with pytest.raises(ValueError):
            SystemdFacts.unit_records(output, "unit")


def test_systemd_property_values_are_not_split_or_dropped():
    assert SystemdCommand.properties("Empty=\nValue=a=b\nState=active\n") == {
        "Empty": "",
        "Value": "a=b",
        "State": "active",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("collector, section", [(SystemdTimesyncFacts, "timesync"), (SystemdResolvedFacts, "status")])
async def test_inactive_systemd_component_is_not_queried(monkeypatch, collector, section):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(Path, "is_dir", lambda path: True)

    async def run(executable, *arguments):
        if "--property=ActiveState" in arguments:
            return "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
        assert "show" in arguments and "timedatectl" in executable
        return "Timezone=UTC\nNTP=no\n"

    monkeypatch.setattr(SystemdCommand, "run", run)
    result = asdict(await collector.collect())
    assert result["available"] and result["manager_available"]
    assert result["service"]["ActiveState"] == "inactive"
    assert result[section] is None


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize("collector", [SystemdTimesyncFacts, SystemdResolvedFacts])
async def test_systemd_component_transfers_independently(tmp_path, collector, facts_docker_protocol):
    result = {collector.key: asdict(await facts_docker_protocol(collector.collect))}
    assert set(result) == {collector.key}
    branch = result[collector.key]
    assert branch["available"] and branch["manager_available"]
    if collector is SystemdTimesyncFacts:
        assert branch["service"]["LoadState"] == "masked"
        assert branch["service"]["ActiveState"] == "inactive"
        assert branch["clock"]["Timezone"]
        assert branch["timesync"] is None
    else:
        assert branch["service"]["ActiveState"] == "active"
        assert branch["status"]


@pytest.mark.docker
@pytest.mark.asyncio
async def test_resolved_stopped_state_and_cache_refresh(tmp_path, facts_docker_protocol):
    local = Cache[Any](versions={c.key: c.version for c in (SystemdResolvedFacts,)})
    cache = JSONCacheDir(tmp_path)
    local.update(await facts.fetch(facts_docker_protocol, sections=local.select(None)))
    await local.save(cache, namespace="container")
    active = {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()}
    assert active["systemd_resolved"]["status"]
    await facts_docker_protocol(Exec.command, "systemctl", "stop", "systemd-resolved.service")
    assert {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()} == active
    local.update(await facts.fetch(facts_docker_protocol, sections=local.select(None)))
    await local.save(cache, namespace="container")
    stopped = {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()}
    assert stopped["systemd_resolved"]["service"]["ActiveState"] == "inactive"
    assert stopped["systemd_resolved"]["status"] is None
    restored = Cache[Any]()
    await restored.load(cache, namespace="container", keys=local.select(None))
    assert {key: asdict(cast(Dataclass, value)) for key, value in restored.data.items()} == stopped
