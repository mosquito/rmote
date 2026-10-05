import errno
import json
import platform
import shutil
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rmote.cache import Cache
from rmote.serialization import Dataclass
from rmote.tools import facts
from rmote.tools.exec import Exec
from rmote.tools.facts.collectors import NetworkdFacts, NetworkFacts
from rmote.tools.facts.collectors import network as network_module


def test_sysfs_interface_fields_and_missing_values(tmp_path):
    interface = tmp_path / "eth0.100"
    interface.mkdir()
    (interface / "ifindex").write_text("42\n")
    (interface / "address").write_text("02:00:00:00:00:01\n")
    (interface / "flags").write_text("0x1003\n")
    (interface / "mtu").write_text("1500\n")
    (interface / "operstate").write_text("up\n")
    statistics = interface / "statistics"
    statistics.mkdir()
    (statistics / "rx_bytes").write_text("1234567890123\n")
    (tmp_path / "disappeared").mkdir()
    result = NetworkFacts.read_interfaces(tmp_path)
    assert result is not None and set(result) == {"eth0.100"}
    assert result["eth0.100"]["ifindex"] == 42
    assert result["eth0.100"]["flags"] == 0x1003
    assert result["eth0.100"]["mtu"] == 1500
    assert result["eth0.100"]["speed"] is None
    assert result["eth0.100"]["statistics"]["rx_bytes"] == 1234567890123
    assert NetworkFacts.read_interfaces(tmp_path / "missing") is None


@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENODEV, errno.EOPNOTSUPP, errno.ENOENT])
def test_unsupported_sysfs_attribute(tmp_path, monkeypatch, error):
    def fail(*args, **kwargs):
        raise OSError(error, "unavailable")

    monkeypatch.setattr(Path, "read_text", fail)
    assert NetworkFacts.read_attribute(tmp_path / "speed") is None


def test_sysfs_permission_error_is_not_hidden(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(PermissionError):
        NetworkFacts.read_attribute(tmp_path / "speed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [
        "null",
        '{"type":"b","data":[true]}',
        '{"type":"s","data":[]}',
        '{"type":"s","data":[42]}',
        '{"type":"s","data":["a","b"]}',
    ],
)
async def test_networkd_rejects_invalid_bus_reply(monkeypatch, envelope):
    async def run(*args, **kwargs):
        return SimpleNamespace(stdout=envelope)

    monkeypatch.setattr(network_module, "async_process", run)
    with pytest.raises(ValueError):
        await NetworkdFacts.call("busctl", "s", "service", "/object", "interface", "Describe")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["platform", "bus", "owner"])
async def test_networkd_unavailable_does_not_describe(monkeypatch, source):
    monkeypatch.setattr(platform, "system", lambda: "Darwin" if source == "platform" else "Linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/busctl")
    monkeypatch.delenv("DBUS_SYSTEM_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(Path, "exists", lambda path: source != "bus")

    async def run(*args, **kwargs):
        assert source == "owner"
        assert "NameHasOwner" in args
        assert "--auto-start=no" in args
        assert "--allow-interactive-authorization=no" in args
        return SimpleNamespace(stdout='{"type":"b","data":[false]}')

    monkeypatch.setattr(network_module, "async_process", run)
    assert asdict(await NetworkdFacts.collect()) == {"available": False, "busctl_available": True, "status": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [[], {}, {"Interfaces": [None]}])
async def test_networkd_invalid_description_propagates(monkeypatch, status):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/busctl")
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/custom/bus")
    monkeypatch.setattr(Path, "exists", lambda path: False)

    async def run(*args, **kwargs):
        data = {"type": "b", "data": [True]} if "NameHasOwner" in args else {"type": "s", "data": [json.dumps(status)]}
        return SimpleNamespace(stdout=json.dumps(data))

    monkeypatch.setattr(network_module, "async_process", run)
    with pytest.raises(ValueError):
        asdict(await NetworkdFacts.collect())


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize("facts_systemd_image", ["debian:forky-slim", "ubuntu:noble"], indirect=True)
async def test_networkd_and_iproute_coexist_and_refresh(facts_docker_protocol, cache):
    local = Cache[Any](
        versions={
            c.key: c.version
            for c in (
                NetworkdFacts,
                NetworkFacts,
            )
        }
    )
    local.update(await facts.fetch(facts_docker_protocol, sections=local.select(None)))
    await local.save(cache, namespace="container")
    active = {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()}
    assert active["network"]["raw"]["iproute"]["available"]
    branch = active["networkd"]
    assert branch["available"] and branch["busctl_available"]
    link = next(link for link in branch["status"]["Interfaces"] if link["Name"] == "facts0")
    assert link["Index"] == active["network"]["interfaces"]["facts0"]["ifindex"]
    assert link["AdministrativeState"] == "configured"
    assert any(item["Address"] == [192, 0, 2, 10] for item in link["Addresses"])
    assert any(item["Family"] == 10 for item in link["Addresses"])
    assert any(item["Table"] == 100 for item in link["Routes"])
    assert any(item["Priority"] == 12345 for item in branch["status"]["RoutingPolicyRules"])
    assert any(item["Address"] == [192, 0, 2, 53] for item in link["DNS"])
    assert link["NTP"]
    assert link["SearchDomains"]
    assert {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()} == active
    # A stopped networkd must not prevent collection through ip.
    await facts_docker_protocol(
        Exec.command, "systemctl", "stop", "systemd-networkd.service", "systemd-networkd.socket"
    )
    assert {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()} == active
    local.update(await facts.fetch(facts_docker_protocol, sections=local.select(None)))
    await local.save(cache, namespace="container")
    stopped = {key: asdict(cast(Dataclass, value)) for key, value in local.data.items()}
    assert stopped["networkd"] == {"available": False, "busctl_available": True, "status": None}
    assert stopped["network"]["raw"]["iproute"]["available"]
    assert stopped["network"]["ipv4"]["addresses"]
    restored = Cache[Any]()
    await restored.load(cache, namespace="container", keys=local.select(None))
    assert {key: asdict(cast(Dataclass, value)) for key, value in restored.data.items()} == stopped
