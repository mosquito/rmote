import asyncio
import shutil
import subprocess
import sys
from dataclasses import asdict
from typing import Any, cast

import pytest

from rmote.cache import Cache
from rmote.serialization import Dataclass
from rmote.tools import facts
from rmote.tools.facts.collectors import (
    AptFacts,
    NetworkdFacts,
    NetworkFacts,
    PacmanFacts,
    SystemdFacts,
    SystemdResolvedFacts,
    SystemdTimesyncFacts,
)


@pytest.mark.asyncio
async def test_absent_binaries_do_not_spawn(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("must not spawn when unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    facts = asdict(await NetworkFacts.collect())
    assert facts["ipv4"]["addresses"] is facts["ipv6"]["routes"] is None
    assert (asdict(await NetworkdFacts.collect()))["available"] is False
    facts = asdict(await SystemdFacts.collect())
    assert facts["available"] is False
    assert facts["manager"] is facts["units"] is facts["unit_files"] is None
    assert (asdict(await SystemdTimesyncFacts.collect()))["available"] is False
    assert (asdict(await SystemdResolvedFacts.collect()))["available"] is False


@pytest.mark.asyncio
async def test_command_failures_propagate(tmp_path):
    # A real failing executable: no fake successful output masking failures.
    with pytest.raises(subprocess.CalledProcessError):
        await NetworkFacts.ip_json(sys.executable, "-c", "raise SystemExit(7)")
    with pytest.raises(subprocess.CalledProcessError):
        await NetworkdFacts.call(sys.executable, "s", "service", "/object", "interface", "Describe")
    with pytest.raises(ValueError):
        await NetworkFacts.ip_json(sys.executable, "-c", "print('{}')")
    with pytest.raises(subprocess.CalledProcessError):
        await SystemdFacts.systemctl(sys.executable, "show")


@pytest.mark.docker
@pytest.mark.asyncio
async def test_collectors_in_systemd_container(facts_docker_protocol, cache):
    local = Cache[Any](versions={c.key: c.version for c in facts.DEFAULT_COLLECTORS})
    local.update(await facts.fetch(facts_docker_protocol, sections=local.select(None)))
    await local.save(cache, namespace="container")
    snapshot = {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()}
    net = snapshot["network"]
    assert net["available"] and net["raw"]["iproute"]["available"]
    assert net["interfaces"]["lo"]["mtu"] > 0
    assert "rx_bytes" in net["raw"]["sysfs"]["interfaces"]["lo"]["statistics"]
    assert any(address["address"] == "127.0.0.1" for address in net["ipv4"]["addresses"])
    assert any(address["address"] == "::1" for address in net["ipv6"]["addresses"])
    for family, address in [("ipv4", "192.0.2.10"), ("ipv6", "2001:db8::10")]:
        assert any(item["address"] == address for item in net[family]["addresses"])
        assert any(
            str(route.get("table")) == "100" and route.get("dev") == "facts0"
            for route in net["raw"]["iproute"][family]["routes"]
        )
        assert any(
            rule.get("priority") == 12345 and str(rule.get("table")) == "100"
            for rule in net["raw"]["iproute"][family]["rules"]
        )
    assert net["interfaces"]["facts0"]["mtu"] > 0
    assert net["ipv4"]["routes"] and net["ipv6"]["routes"]
    assert net["raw"]["iproute"]["ipv4"]["rules"] and net["raw"]["iproute"]["ipv6"]["rules"]
    assert snapshot["networkd"]["available"]
    assert any(link["Name"] == "facts0" for link in snapshot["networkd"]["status"]["Interfaces"])
    manager = snapshot["systemd"]
    assert manager["available"] and manager["manager_available"]
    assert manager["version"].startswith("systemd ")
    assert manager["manager"]["SystemState"]
    assert manager["units"]["basic.target"]["load"] == "loaded"
    assert manager["unit_files"]["basic.target"]["state"]
    timesync = snapshot["systemd_timesync"]
    assert timesync["available"] and timesync["manager_available"]
    assert timesync["clock"]["Timezone"]
    assert timesync["service"]["ActiveState"] == "inactive"
    assert timesync["timesync"] is None
    resolved = snapshot["systemd_resolved"]
    assert resolved["available"] and resolved["manager_available"]
    assert any("servers" in record for record in resolved["status"])
    assert snapshot["apt"]["available"]
    assert snapshot["apt"]["packages"]["dpkg"]["version"]
    assert snapshot["pacman"]["available"] is False
    assert {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()} == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("collector", [AptFacts, PacmanFacts])
async def test_package_query_failures_propagate(monkeypatch, tmp_path, collector):
    executable = tmp_path / "broken-package-tool"
    executable.write_text("#!/bin/sh\necho broken >&2\nexit 7\n")
    executable.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(executable))
    with pytest.raises(subprocess.CalledProcessError) as error:
        asdict(await collector.collect())
    assert error.value.returncode == 7
    assert error.value.stderr == "broken\n"


@pytest.mark.docker
@pytest.mark.asyncio
async def test_primary_exits_in_disposable_network_namespace(facts_docker_protocol, cache):
    from rmote.tools.exec import Exec

    # Only the disposable container's network changes; the host is untouched.
    for family, gateway in (("-4", "192.0.2.1"), ("-6", "2001:db8::1")):
        await facts_docker_protocol(
            Exec.command, "ip", family, "route", "add", "default", "via", gateway, "dev", "facts0", "metric", "50"
        )
    local = Cache[Any](versions={c.key: c.version for c in (NetworkFacts,)})
    local.update(await facts.fetch(facts_docker_protocol, sections=["network"]))
    await local.save(cache, namespace="container")
    snapshot = local.data
    network = snapshot["network"]
    assert network.default.ipv4 is not None and network.default.ipv6 is not None
    assert network.default.ipv4.interface == "facts0"
    assert network.default.ipv4.address == "192.0.2.10"
    assert network.default.ipv4.gateway == "192.0.2.1"
    assert network.default.ipv6.address == "2001:db8::10"
    assert network.default.ipv6.gateway == "2001:db8::1"
    restored = Cache[Any]()
    await restored.load(cache, namespace="container")
    saved = restored.data
    assert saved["network"].default == network.default
