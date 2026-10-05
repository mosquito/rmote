"""Platform-neutral network models collected from Linux sources, plus networkd."""

import errno
import ipaddress
import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rmote.process import async_process
from rmote.protocol import Tool


@dataclass
class NetworkInterface:
    ifindex: int
    mac: str | None
    state: str | None
    mtu: int | None
    speed_bps: int | None


@dataclass
class NetworkAddress:
    interface: str
    ifindex: int
    address: str
    prefixlen: int


@dataclass
class NetworkNextHop:
    interface: str | None
    ifindex: int | None
    gateway: str | None
    weight: int | None


@dataclass
class NetworkRoute:
    destination: str
    interface: str | None
    ifindex: int | None
    gateway: str | None
    metric: int
    nexthops: list[NetworkNextHop]


@dataclass
class NetworkFamily:
    addresses: list[NetworkAddress] | None = None
    routes: list[NetworkRoute] | None = None


@dataclass
class DNSServer:
    address: str
    interface: str | None
    ifindex: int | None


@dataclass
class DefaultRoute:
    interface: str
    ifindex: int
    address: str | None
    prefixlen: int | None
    gateway: str | None
    metric: int


@dataclass
class NetworkDefaults:
    ipv4: DefaultRoute | None = None
    ipv6: DefaultRoute | None = None


@dataclass
class NetworkInfo:
    available: bool = False
    interfaces: dict[str, NetworkInterface] | None = None
    ipv4: NetworkFamily = field(default_factory=NetworkFamily)
    ipv6: NetworkFamily = field(default_factory=NetworkFamily)
    default: NetworkDefaults = field(default_factory=NetworkDefaults)
    dns: list[DNSServer] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkdInfo:
    available: bool = False
    busctl_available: bool = False
    status: dict[str, Any] | None = None


class NetworkFacts(Tool):
    """Collect Linux network state in a JSON-compatible schema.

    Owns ``network``, schema version 1. ``interfaces`` maps names to ``ifindex``,
    ``mac`` (lowercase colon-separated), ``state``, ``mtu`` (bytes) and
    ``speed_bps`` (transmit link speed). Missing properties are None.
    ``ipv4``/``ipv6`` contain flat ``addresses`` and ``routes`` arrays:

    * Address: ``interface``, ``ifindex``, ``address``, ``prefixlen``.
    * Route: ``destination`` (CIDR), ``interface``, ``ifindex``, ``gateway``
      (None for on-link), ``metric`` and ``nexthops``. Multipath routes retain
      each next hop's ``interface``, ``ifindex``, ``gateway`` and ``weight``;
      ordinary routes have an empty next-hop array. Metric is the route metric.
    * ``dns``: a list of ``address``, ``interface`` and ``ifindex`` records,
      read from /etc/resolv.conf, which can be a local stub with no interface.

    ``raw.sysfs.interfaces`` preserves interface properties and statistics.
    ``raw.iproute.ipv4``/``ipv6`` preserve addresses, routes from all tables and
    policy rules; ``raw.iproute.available`` reports the ip command.
    ``raw.resolv_conf`` retains the parsed resolver endpoints. Common routes alone are
    insufficient to reproduce routing policy. ``networkd`` remains an
    independent collector.

    Collection is implemented only on Linux. Other platforms return
    ``NetworkInfo(available=False)`` with None sections, empty ``raw`` and
    no default exits; the IPv4/IPv6 family objects still exist.
    A returned Linux result has ``available=True`` even when individual
    sources are absent. Without ip, available sysfs and resolver data are
    still collected. Unavailable sections are None;
    successful empty queries return empty lists or mappings. Errors from
    available sources propagate, never becoming empty facts. Linux uses sysfs
    and iproute2 JSON. No Python dependencies or network configuration changes
    are needed.
    """

    key = "network"
    version = 1
    result_type = NetworkInfo

    @staticmethod
    def read_attribute(path: Path) -> str | None:
        """Read an optional sysfs attribute; unsupported/down-device values are None.

        Sysfs can report EINVAL or EOPNOTSUPP for speed/duplex/carrier. Disappearing
        interfaces are normal during collection. Permission and other I/O errors
        propagate instead of silently looking like missing facts.
        """
        try:
            return path.read_text().strip()
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENODEV, errno.EINVAL, errno.EOPNOTSUPP):
                return None
            raise

    @staticmethod
    def read_interfaces(root: Path = Path("/sys/class/net")) -> dict[str, Any] | None:
        """Read interface properties and statistics directly, without invoking ip."""
        if not root.is_dir():
            return None
        result = {}
        for path in sorted(root.iterdir()):
            ifindex = NetworkFacts.read_attribute(path / "ifindex")
            if ifindex is None:
                continue
            interface: dict[str, Any] = {"ifindex": int(ifindex)}
            for name in ("address", "broadcast", "operstate", "duplex", "ifalias"):
                interface[name] = NetworkFacts.read_attribute(path / name)
            for name in ("mtu", "iflink", "type", "speed", "carrier", "tx_queue_len", "flags"):
                value = NetworkFacts.read_attribute(path / name)
                interface[name] = int(value, 16 if name == "flags" else 10) if value is not None else None
            driver = path / "device" / "driver"
            interface["driver"] = driver.resolve().name if driver.exists() else None
            master = path / "master"
            interface["master"] = master.resolve().name if master.exists() else None
            statistics = path / "statistics"
            try:
                counters = sorted(statistics.iterdir())
            except FileNotFoundError:
                interface["statistics"] = None
            else:
                interface["statistics"] = {
                    counter.name: int(value) if (value := NetworkFacts.read_attribute(counter)) is not None else None
                    for counter in counters
                }
            result[path.name] = interface
        return result

    @staticmethod
    async def ip_json(executable: str, *arguments: str) -> list[dict[str, Any]]:
        """Run a read-only ip query with a 30-second deadline and validate its JSON."""
        output = (
            await async_process(
                executable,
                *arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        ).stdout
        value = json.loads(output)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError("Expected a JSON array of objects from ip")
        return value

    @staticmethod
    def empty_data() -> dict[str, Any]:
        """Return the same shape even when a source is unavailable."""
        return {
            "available": False,
            "interfaces": None,
            "ipv4": {"addresses": None, "routes": None},
            "ipv6": {"addresses": None, "routes": None},
            "dns": None,
            "raw": {},
        }

    @staticmethod
    def interface(index: int) -> dict[str, Any]:
        """Unknown properties stay null, not zero or an invented down state."""
        return {"ifindex": index, "mac": None, "state": None, "mtu": None, "speed_bps": None}

    @staticmethod
    def resolv_conf(path: Path = Path("/etc/resolv.conf")) -> list[dict[str, Any]] | None:
        """Read resolver endpoints, which may be local stubs rather than upstream DNS."""
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        servers = []
        for line in text.splitlines():
            words = line.split("#", 1)[0].split(";", 1)[0].split()
            if len(words) >= 2 and words[0] == "nameserver":
                servers.append({"address": str(ipaddress.ip_address(words[1])), "interface": None, "ifindex": None})
        return servers

    @staticmethod
    def normalize(raw: dict[str, Any], *, keep: bool = True) -> NetworkInfo:
        """Keep sysfs/iproute2 details while exposing platform-neutral records.

        Without *keep* the texts are used to build the records and then
        dropped, so the branch carries the records alone.
        """
        result = NetworkFacts.empty_data()
        result.update(available=True, raw=raw, dns=raw["resolv_conf"])
        sysfs = raw["sysfs"]["interfaces"]
        interfaces: dict[str, Any] | None = None if sysfs is None else {}
        for name, item in (sysfs or {}).items():
            interface = NetworkFacts.interface(item["ifindex"])
            speed = item.get("speed")
            interface.update(
                mac=item.get("address"),
                state=item.get("operstate"),
                mtu=item.get("mtu"),
                speed_bps=speed * 1_000_000 if speed is not None and speed >= 0 else None,
            )
            assert interfaces is not None
            interfaces[name] = interface
        for family, version in (("ipv4", 4), ("ipv6", 6)):
            data = raw["iproute"][family]
            if data is None:
                continue
            if interfaces is None:
                interfaces = {}
            addresses = []
            for link in data["addresses"]:
                name, index = link["ifname"], link["ifindex"]
                if name not in interfaces:
                    interfaces[name] = NetworkFacts.interface(index)
                    interfaces[name].update(
                        mac=link.get("address"), mtu=link.get("mtu"), state=link.get("operstate", "unknown").lower()
                    )
                for address in link.get("addr_info", []):
                    addresses.append(
                        {
                            "interface": name,
                            "ifindex": index,
                            "address": str(ipaddress.ip_address(address["local"])),
                            "prefixlen": address["prefixlen"],
                        }
                    )
            routes = []
            for route in data["routes"]:
                name = route.get("dev")
                hops = []
                for hop in route.get("nexthops", []):
                    hop_name = hop.get("dev")
                    hops.append(
                        {
                            "interface": hop_name,
                            "ifindex": interfaces.get(hop_name, {}).get("ifindex"),
                            "gateway": hop.get("gateway"),
                            "weight": hop.get("weight"),
                        }
                    )
                destination = route.get("dst", "default")
                if destination == "default":
                    destination = "0.0.0.0/0" if version == 4 else "::/0"
                routes.append(
                    {
                        "destination": str(ipaddress.ip_network(destination, strict=False)),
                        "interface": name,
                        "ifindex": interfaces.get(name, {}).get("ifindex"),
                        "gateway": route.get("gateway"),
                        "metric": route.get("metric", 0),
                        "nexthops": hops,
                    }
                )
            result[family] = {"addresses": addresses, "routes": routes}
        result["interfaces"] = interfaces
        info = NetworkFacts.model(result)
        if not keep:
            # The default route is read from the texts, so they are dropped
            # only after the records are built.
            info.raw = {}
        return info

    @staticmethod
    def model(data: dict[str, Any]) -> NetworkInfo:
        """Build typed common records while retaining native data unchanged."""
        families = {}
        for name in ("ipv4", "ipv6"):
            addresses, routes = data[name]["addresses"], data[name]["routes"]
            families[name] = NetworkFamily(
                None if addresses is None else [NetworkAddress(**item) for item in addresses],
                None
                if routes is None
                else [
                    NetworkRoute(
                        **{key: value for key, value in item.items() if key != "nexthops"},
                        nexthops=[NetworkNextHop(**hop) for hop in item["nexthops"]],
                    )
                    for item in routes
                ],
            )
        result = NetworkInfo(
            available=data["available"],
            interfaces=None
            if data["interfaces"] is None
            else {name: NetworkInterface(**item) for name, item in data["interfaces"].items()},
            ipv4=families["ipv4"],
            ipv6=families["ipv6"],
            dns=None if data["dns"] is None else [DNSServer(**item) for item in data["dns"]],
            raw=data["raw"],
        )
        result.default = NetworkDefaults(
            NetworkFacts.default_route(result, "ipv4"), NetworkFacts.default_route(result, "ipv6")
        )
        return result

    @staticmethod
    def default_route(info: NetworkInfo, family: str) -> DefaultRoute | None:
        """Choose a representative main-table exit, not a destination policy lookup."""
        data = info.ipv4 if family == "ipv4" else info.ipv6
        iproute = info.raw.get("iproute")
        if iproute is None or iproute[family] is None:
            return None
        native = iproute[family]["routes"]
        candidates: list[tuple[int, int, str, NetworkRoute, NetworkNextHop, dict[str, Any]]] = []
        for route, item in zip(data.routes or [], native, strict=True):
            if route.destination != ("0.0.0.0/0" if family == "ipv4" else "::/0"):
                continue
            if (
                item.get("table", "main") not in ("main", 254, "254")
                or item.get("type", "unicast") != "unicast"
                or {"dead", "linkdown"}.intersection(item.get("flags", []))
            ):
                continue
            metric = route.metric
            hops = route.nexthops or [NetworkNextHop(route.interface, route.ifindex, route.gateway, None)]
            for hop in hops:
                interface = (info.interfaces or {}).get(hop.interface or "")
                if interface is None or interface.state in ("down", "notpresent", "lowerlayerdown"):
                    continue
                candidates.append((metric, interface.ifindex, hop.gateway or "", route, hop, item))
        if not candidates:
            return None
        _, index, _, route, hop, native_route = min(candidates, key=lambda candidate: candidate[:3])
        addresses = [address for address in data.addresses or [] if address.ifindex == index]
        eligible = []
        for address in addresses:
            ip = ipaddress.ip_address(address.address)
            if ip.is_unspecified or ip.is_multicast:
                continue
            native_address: dict[str, Any] = next(
                (
                    row
                    for link in iproute[family]["addresses"]
                    if link["ifindex"] == index
                    for row in link.get("addr_info", [])
                    if row["local"] == address.address
                ),
                {},
            )
            if any(native_address.get(flag) for flag in ("tentative", "dadfailed", "deprecated")):
                continue
            if {"tentative", "dadfailed", "deprecated"}.intersection(native_address.get("flags", [])):
                continue
            eligible.append(address)
        preferred = native_route.get("prefsrc")
        selected = min(
            eligible,
            key=lambda address: (
                address.address != preferred,
                ipaddress.ip_address(address.address).is_link_local,
                int(ipaddress.ip_address(address.address)),
                address.prefixlen,
            ),
            default=None,
        )
        assert hop.interface is not None
        return DefaultRoute(
            hop.interface,
            index,
            selected.address if selected else None,
            selected.prefixlen if selected else None,
            hop.gateway,
            route.metric,
        )

    @staticmethod
    async def collect(*, raw: bool = True) -> NetworkInfo:
        """Collect read-only observations; separate queries are not a transaction.

        Without *raw* the branch keeps every parsed record and drops the sysfs
        properties, the iproute2 answers and the resolver file.
        """
        if platform.system() != "Linux":
            return NetworkInfo()
        executable = shutil.which("ip")
        texts: dict[str, Any] = {
            "sysfs": {"interfaces": NetworkFacts.read_interfaces()},
            "iproute": {"available": executable is not None, "ipv4": None, "ipv6": None},
            "resolv_conf": NetworkFacts.resolv_conf(),
        }
        if executable is not None:
            for key, family in (("ipv4", "-4"), ("ipv6", "-6")):
                texts["iproute"][key] = {
                    "addresses": await NetworkFacts.ip_json(executable, family, "-j", "address", "show"),
                    "routes": await NetworkFacts.ip_json(executable, family, "-j", "route", "show", "table", "all"),
                    "rules": await NetworkFacts.ip_json(executable, family, "-j", "rule", "show"),
                }
        return NetworkFacts.normalize(texts, keep=raw)


class NetworkdFacts(Tool):
    """Collect systemd-networkd's network description through D-Bus.

    Owns ``networkd``, independently of :class:`NetworkFacts` and its cache.
    ``busctl_available`` reports the binary; ``available`` reports a running
    networkd on the system bus. Without Linux, busctl, a system bus or a running
    daemon, ``status`` is None. The standard system bus socket or an explicit
    ``DBUS_SYSTEM_BUS_ADDRESS`` is required. Collection never starts networkd.

    ``status`` preserves the JSON object returned by network1.Manager.Describe:
    ``Interfaces`` contains link state, configuration sources, addresses,
    routes, DNS/NTP and DHCP details where reported; global fields include
    routing policy rules. Field availability depends on systemd and interface
    configuration. Addresses retain networkd's byte-array representation.
    Requires the Describe API (tested with systemd 255 and newer), independently
    of networkctl's CLI output formats. Unsupported APIs, malformed output,
    permission errors and timeouts propagate without replacing cached facts.

    Uses two read-only busctl calls: owner discovery and one description query.
    Each has a 30-second limit and disables activation and interactive
    authorization. No root access is required. This daemon view and the kernel
    view in ``network`` are separate observations, not an atomic snapshot.
    """

    key = "networkd"
    version = 1
    result_type = NetworkdInfo

    @staticmethod
    async def call(executable: str, signature: str, *arguments: str) -> Any:
        """Read a single typed D-Bus return value from busctl's JSON envelope."""
        output = await async_process(
            executable,
            "--system",
            "--json=short",
            "--auto-start=no",
            "--allow-interactive-authorization=no",
            "call",
            *arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        envelope = json.loads(output.stdout)
        if not isinstance(envelope, dict) or envelope.get("type") != signature:
            raise ValueError(f"Expected D-Bus signature {signature} from busctl")
        data = envelope.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError("Expected one D-Bus return value from busctl")
        value = data[0]
        if (signature == "b" and type(value) is not bool) or (signature == "s" and not isinstance(value, str)):
            raise ValueError(f"Invalid D-Bus value for signature {signature}")
        return value

    @staticmethod
    async def collect() -> NetworkdInfo:
        """Describe an already running networkd, retaining its complete JSON."""
        executable = shutil.which("busctl")
        result = NetworkdInfo(busctl_available=executable is not None)
        if platform.system() != "Linux" or executable is None:
            return result
        if not os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") and not Path("/run/dbus/system_bus_socket").exists():
            return result
        owner = await NetworkdFacts.call(
            executable,
            "b",
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            "s",
            "org.freedesktop.network1",
        )
        if not owner:
            return result
        description = await NetworkdFacts.call(
            executable,
            "s",
            "org.freedesktop.network1",
            "/org/freedesktop/network1",
            "org.freedesktop.network1.Manager",
            "Describe",
        )
        status = json.loads(description)
        if not isinstance(status, dict) or not isinstance(status.get("Interfaces"), list):
            raise ValueError("Expected a networkd description with an Interfaces array")
        if any(not isinstance(item, dict) for item in status["Interfaces"]):
            raise ValueError("Expected networkd interface objects")
        result.available = True
        result.status = status
        return result


__tool_package__ = "rmote.tools.facts"
