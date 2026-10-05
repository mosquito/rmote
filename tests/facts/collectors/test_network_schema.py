"""Common network records must not depend on their operating-system source."""

import copy
import platform
import shutil
from dataclasses import asdict
from typing import Any

import pytest

from rmote.tools.facts.collectors import NetworkFacts
from rmote.tools.facts.collectors import network as network_module


def linux() -> dict[str, Any]:
    return {
        "sysfs": {
            "interfaces": {
                "Сеть": {
                    "ifindex": 7,
                    "address": "02:00:00:00:00:01",
                    "operstate": "up",
                    "mtu": 1500,
                    "speed": 1000,
                    "flags": 4099,
                }
            }
        },
        "iproute": {
            "available": True,
            "ipv4": {
                "addresses": [
                    {"ifindex": 7, "ifname": "Сеть", "addr_info": [{"local": "192.0.2.10", "prefixlen": 24}]}
                ],
                "routes": [{"dst": "default", "dev": "Сеть", "gateway": "192.0.2.1", "metric": 20}],
                "rules": [{"priority": 1000, "table": 100}],
            },
            "ipv6": {
                "addresses": [
                    {"ifindex": 7, "ifname": "Сеть", "addr_info": [{"local": "2001:db8::10", "prefixlen": 64}]}
                ],
                "routes": [{"dst": "2001:db8::/64", "dev": "Сеть", "metric": 20}],
                "rules": [],
            },
        },
        "resolv_conf": [{"address": "192.0.2.53", "interface": None, "ifindex": None}],
    }


def test_sources_preserve_native_records_without_changing_input():
    raw = linux()
    raw["sysfs"]["interfaces"]["Сеть"]["statistics"] = {"rx_bytes": 123}
    before = copy.deepcopy(raw)
    result = NetworkFacts.normalize(raw)
    assert result.raw == before == raw
    assert set(result.raw) == {"iproute", "sysfs", "resolv_conf"}
    assert result.raw["iproute"]["ipv4"]["rules"] == [{"priority": 1000, "table": 100}]
    assert result.raw["sysfs"]["interfaces"]["Сеть"]["statistics"] == {"rx_bytes": 123}
    assert result.dns is not None
    assert asdict(result.dns[0]) == raw["resolv_conf"][0]
    assert result.default.ipv4 is not None
    assert result.default.ipv4.gateway == "192.0.2.1"


def test_multipath_and_host_routes_preserve_semantics():
    raw = linux()
    raw["iproute"]["ipv4"]["routes"] = [
        {"dst": "192.0.2.10", "type": "local", "dev": "Сеть"},
        {
            "dst": "default",
            "nexthops": [
                {"gateway": "192.0.2.1", "dev": "Сеть", "weight": 2},
                {"gateway": "198.51.100.1", "dev": "gone", "weight": 1},
            ],
        },
    ]
    result = asdict(NetworkFacts.normalize(raw))
    routes = result["ipv4"]["routes"]
    assert routes[0]["destination"] == "192.0.2.10/32"
    assert routes[1]["interface"] is None and routes[1]["gateway"] is None
    assert routes[1]["nexthops"] == [
        {"gateway": "192.0.2.1", "interface": "Сеть", "ifindex": 7, "weight": 2},
        {"gateway": "198.51.100.1", "interface": "gone", "ifindex": None, "weight": 1},
    ]
    assert result["raw"]["iproute"]["ipv4"]["routes"][0]["type"] == "local"


def test_missing_sysfs_uses_ip_interfaces():
    raw = linux()
    raw["sysfs"]["interfaces"] = None
    result = asdict(NetworkFacts.normalize(raw))
    assert result["interfaces"]["Сеть"]["ifindex"] == 7
    assert result["interfaces"]["Сеть"]["speed_bps"] is None
    assert result["ipv4"]["routes"][0]["ifindex"] == 7


def test_resolver_endpoints_are_not_claimed_as_upstream(tmp_path):
    path = tmp_path / "resolv.conf"
    path.write_text("# managed\nnameserver 127.0.0.53 # local stub\nnameserver fe80::1%eth0\nsearch example.org\n")
    assert NetworkFacts.resolv_conf(path) == [
        {"address": "127.0.0.53", "interface": None, "ifindex": None},
        {"address": "fe80::1%eth0", "interface": None, "ifindex": None},
    ]
    assert NetworkFacts.resolv_conf(tmp_path / "absent") is None
    path.write_text("")
    assert NetworkFacts.resolv_conf(path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("system", ["Linux", "Darwin"])
async def test_missing_commands_and_unsupported_platform(monkeypatch, system):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(NetworkFacts, "read_interfaces", lambda: linux()["sysfs"]["interfaces"])
    monkeypatch.setattr(NetworkFacts, "resolv_conf", lambda: [])

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not launch missing commands")

    monkeypatch.setattr(network_module, "async_process", forbidden)
    result = asdict(await NetworkFacts.collect())
    assert result["available"] is (system == "Linux")
    for family in ("ipv4", "ipv6"):
        assert result[family] == {"addresses": None, "routes": None}
    assert result["interfaces"] is not None if system == "Linux" else result["interfaces"] is None
    if system == "Linux":
        assert result["raw"] == {
            "sysfs": {"interfaces": linux()["sysfs"]["interfaces"]},
            "iproute": {"available": False, "ipv4": None, "ipv6": None},
            "resolv_conf": [],
        }
    else:
        assert result["raw"] == {}
    assert result["default"] == {"ipv4": None, "ipv6": None}


def test_default_linux_main_table_metrics_source_and_absence():
    raw = linux()
    raw["iproute"]["ipv4"]["routes"] += [
        {"dst": "default", "dev": "Сеть", "gateway": "192.0.2.99", "table": 100},
        {"dst": "default", "dev": "Сеть", "gateway": "192.0.2.2", "metric": 10, "prefsrc": "192.0.2.20"},
        {"dst": "default", "type": "blackhole", "metric": 1},
    ]
    raw["iproute"]["ipv4"]["addresses"][0]["addr_info"] += [{"local": "192.0.2.20", "prefixlen": 24}]
    raw["iproute"]["ipv6"]["routes"] += [{"dst": "default", "dev": "Сеть", "gateway": "fe80::1", "metric": 100}]
    result = NetworkFacts.normalize(raw)
    assert result.default.ipv4 is not None
    assert result.default.ipv4.gateway == "192.0.2.2"
    assert result.default.ipv4.address == "192.0.2.20"
    assert result.default.ipv6 is not None
    assert result.default.ipv6.address == "2001:db8::10"
    assert result.default.ipv6.gateway == "fe80::1"
    assert result.ipv4.routes is not None
    assert len(result.ipv4.routes) == 4
    raw["iproute"]["ipv4"]["addresses"][0]["addr_info"] = []
    empty_address = NetworkFacts.normalize(raw).default.ipv4
    assert empty_address is not None and empty_address.address is None
    raw["sysfs"]["interfaces"]["Сеть"]["operstate"] = "down"
    assert NetworkFacts.normalize(raw).default.ipv4 is None


def test_default_multipath_and_unusable_addresses():
    raw = linux()
    raw["iproute"]["ipv4"]["routes"] = [
        {
            "dst": "default",
            "nexthops": [
                {"dev": "Сеть", "gateway": "192.0.2.2"},
                {"dev": "Сеть", "gateway": "192.0.2.1"},
            ],
        }
    ]
    raw["iproute"]["ipv4"]["addresses"][0]["addr_info"][0]["tentative"] = True
    result = NetworkFacts.normalize(raw)
    assert result.default.ipv4 is not None
    assert result.default.ipv4.gateway == "192.0.2.1"
    assert result.default.ipv4.address is None
    assert result.ipv4.routes is not None
    assert len(result.ipv4.routes[0].nexthops) == 2
