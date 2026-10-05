# Facts

Collect a host's current state with `from rmote.tools import facts` and
`await facts.fetch(remote, sections=["system", "network"])`. Each collector is an
ordinary Tool, and `fetch` calls every selected one in its own RPC, so a branch
arrives as soon as the target produces it. `rmote.cache.Cache` lives on the
controller. Each collector owns a top-level key, so refreshing one branch
preserves the others.

`facts.gather` collects on the side that runs it. Pass it through RPC
(`await remote(facts.gather)`) to do the whole collection on the target in one
call and one answer, or call it directly to collect the local host.

## What `available` means

Every branch carries `available`. It reports whether the branch read the kernel
interface it is built on, which today means Linux. It does **not** mean "this
branch has no data": a branch that can answer through the standard library does
so whatever `available` says.

A target that is POSIX but not Linux therefore reports this much:

| Branch | Reported without the Linux interface | Source |
| --- | --- | --- |
| `cpu` | `architecture`, `usable`, `topology.logical` | `platform.machine`, `os.cpu_count`, `sysconf` |
| `memory` | `total_bytes`, and `free_bytes` where the name exists | `sysconf` |
| `storage` | `root`, the space of the root filesystem | `os.statvfs` |
| `system`, `python` | everything; neither needs a kernel interface | standard library |

The other branches report `available=False` and nothing else. `apt`, `pacman`,
`systemd*` and `networkd` describe software the target does not have. `network`
reads Linux sysfs and `ip`, and no portable call replaces them. No collector
raises on any platform: an absent source gives None, not an error.

A collector does not run an external command to fill a gap. `sysctl`,
`diskutil` and `system_profiler` would report more on macOS, and the project
reads kernel files and the standard library only.

## Print a snapshot over SSH

From a checkout, run the [SSH facts example](https://github.com/mosquito/rmote/blob/master/examples/ssh-facts.py):

```bash
uv run examples/ssh-facts.py user@server another-server > facts.json
```

Hosts are collected concurrently. Standard output contains one JSON object
keyed by the supplied hostnames, even for a single host. Each host contains
`system`, `cpu`, `memory`, `python`, `network`, `networkd`, `storage`,
`systemd`, `systemd_timesync`, `systemd_resolved`, `apt`, and `pacman`
branches. The script saves them in
`.cache/facts` using `JSONCacheDir` and reuses branches younger than 60 seconds.
Set `--max-age SECONDS` to change this lifetime or `--max-age 0` to refresh all
collectors. When every branch is fresh, the script returns the cached snapshot
without opening SSH. Missing or stale branches share one connection per host;
only those branches are collected again.
Errors go to standard error and produce a nonzero exit status.

System and Python facts use Python's standard library. Network properties
come from Linux sysfs; addresses, routes and rules use `ip`. The common
network schema below describes what they expose.
The independent
`networkd` branch uses `busctl` to read systemd-networkd’s JSON description,
including configuration sources, link state and DNS/NTP settings. Both sources
are collected by default and cached separately. A stopped or missing networkd
leaves its branch unavailable without affecting collection through `ip`.
The networkd collector works on Ubuntu Noble (systemd 255); it does not depend
on `networkctl` JSON output. Systemd and
package collectors check for their required commands before running them.
Missing commands leave the corresponding branch unavailable; they do not
prevent other collectors from running. A command that is present but fails,
returns unsupported output or times out raises an error. Collection reads
state without changing services, packages or repository indexes.

See the {ref}`operations and types <facts-operations-and-types>` below for each collector's fields,
availability flags and requirements.

## Choose collectors

`facts.DEFAULT_COLLECTORS` is the tuple of built-in collector classes used by
`fetch`, `gather` and `validate` when `collectors` is omitted:

| Branch key | Collector | Result model |
| --- | --- | --- |
| `apt` | {class}`~rmote.tools.facts.collectors.apt.AptFacts` | {class}`~rmote.tools.facts.collectors.apt.AptInfo` |
| `cpu` | {class}`~rmote.tools.facts.collectors.cpu.CpuFacts` | {class}`~rmote.tools.facts.collectors.cpu.CpuInfo` |
| `memory` | {class}`~rmote.tools.facts.collectors.memory.MemoryFacts` | {class}`~rmote.tools.facts.collectors.memory.MemoryInfo` |
| `pacman` | {class}`~rmote.tools.facts.collectors.pacman.PacmanFacts` | {class}`~rmote.tools.facts.collectors.pacman.PacmanInfo` |
| `system` | {class}`~rmote.tools.facts.collectors.system.SystemFacts` | {class}`~rmote.tools.facts.collectors.system.SystemInfo` |
| `python` | {class}`~rmote.tools.facts.collectors.python.PythonFacts` | {class}`~rmote.tools.facts.collectors.python.PythonInfo` |
| `network` | {class}`~rmote.tools.facts.collectors.network.NetworkFacts` | {class}`~rmote.tools.facts.collectors.network.NetworkInfo` |
| `networkd` | {class}`~rmote.tools.facts.collectors.network.NetworkdFacts` | {class}`~rmote.tools.facts.collectors.network.NetworkdInfo` |
| `storage` | {class}`~rmote.tools.facts.collectors.storage.StorageFacts` | {class}`~rmote.tools.facts.collectors.storage.StorageInfo` |
| `systemd` | {class}`~rmote.tools.facts.collectors.systemd.SystemdFacts` | {class}`~rmote.tools.facts.collectors.systemd.SystemdInfo` |
| `systemd_timesync` | {class}`~rmote.tools.facts.collectors.systemd.SystemdTimesyncFacts` | {class}`~rmote.tools.facts.collectors.systemd.TimesyncInfo` |
| `systemd_resolved` | {class}`~rmote.tools.facts.collectors.systemd.SystemdResolvedFacts` | {class}`~rmote.tools.facts.collectors.systemd.ResolvedInfo` |

Use `sections=["system", "python"]` to select keys from that default set.
Pass `collectors=(SystemFacts, PythonFacts)` to define a different set instead;
it replaces the default tuple. `sections` then selects only from the supplied
collectors. Import collector classes from their defining modules, as below.

## Refuse the source texts

A branch keeps the text of every source it read in `raw`, so a field the model
does not have can still be read from the snapshot. Those texts are most of a
snapshot: on a host with sixteen processors the `cpu` branch measures 45 385
bytes pickled, of which 41 980 are the text of `/proc/cpuinfo`, and the
`memory` branch measures 1 503 bytes, of which 1 190 are `/proc/meminfo`. The
two branches together shrink 12.6 times without them.

`raw=False` asks every collector that can to parse its sources and drop their
texts. The parsed fields are the same, and for `fetch` the texts never reach
the wire. A collector that does not accept the keyword is collected as it is,
with its texts; `facts.drops_raw(Collector.collect)` answers which is which.

<!-- name: test_facts; case: without_raw -->
```python
import asyncio

from rmote.protocol import Protocol
from rmote.tools import facts
from rmote.tools.facts.collectors import MemoryFacts, PythonFacts


async def lean() -> None:
    async with await Protocol.from_command() as remote:
        branch = await facts.fetch(remote, collectors=(MemoryFacts, PythonFacts), raw=False)
        assert branch["memory"].raw == {}


asyncio.run(lean())
```

This example collects two branches locally and configures a cache with the
same expected schema versions:

<!-- name: test_facts_collector_selection -->
```python
import asyncio
from typing import Any

from rmote.cache import Cache
from rmote.tools import facts
from rmote.tools.facts.collectors.python import PythonFacts, PythonInfo
from rmote.tools.facts.collectors.system import SystemFacts, SystemInfo

collectors = (SystemFacts, PythonFacts)
snapshot = asyncio.run(facts.gather(collectors=collectors))
assert set(snapshot) == {"system", "python"}
assert isinstance(snapshot["system"], SystemInfo)
assert isinstance(snapshot["python"], PythonInfo)

cache = Cache[Any](versions={collector.key: collector.version for collector in collectors})
cache.update(snapshot)
assert facts.validate(cache.data, collectors=collectors) == snapshot
```

For remote collection, pass the same tuple to
`await facts.fetch(remote, collectors=collectors)`.
{class}`~rmote.tools.facts.schema.CollectorRegistry` validates collector
definitions and selects keys for these helpers. Normal callers use the helpers
directly; custom collection code can use the registry to validate its own
branches. It belongs to facts, while the general cache stores values and versions.

## Common network schema

`NetworkFacts` uses a platform-neutral result model. The built-in collector
currently reads network state only on Linux, using sysfs, iproute2 and
`/etc/resolv.conf`. On other operating systems it returns `available=False`,
`interfaces=None`, `dns=None` and an empty `raw`. The `ipv4` and `ipv6` objects
still exist, with `addresses=None` and `routes=None`; both default exits are
`None` too.

On Linux, a returned result has `available=True` even if individual sources
are absent. Check each section for `None` to determine which data was observed.
Interface names remain native (`eth0`,
`enp3s0`, Unicode aliases); they are keys in
`network.interfaces`. Indices identify interfaces within the current host,
not across hosts or interface recreation.

| Field | Meaning |
| --- | --- |
| `available` | Whether the collector supports this platform; currently true only on Linux |
| `interfaces` | Mapping from interface name to `ifindex`, `mac`, `state`, `mtu`, `speed_bps` |
| `ipv4.addresses`, `ipv6.addresses` | Flat lists of `interface`, `ifindex`, `address`, `prefixlen` |
| `ipv4.routes`, `ipv6.routes` | Lists of `destination`, `interface`, `ifindex`, `gateway`, `metric`, `nexthops` |
| `default.ipv4`, `default.ipv6` | One primary exit: `interface`, `ifindex`, `address`, `prefixlen`, `gateway`, `metric`, or null |
| `dns` | Resolver endpoints: `address`, `interface`, `ifindex` |
| `raw.iproute` | `available` and native `ipv4`/`ipv6` addresses, routes and rules from iproute2 |
| `raw.sysfs.interfaces` | Interface properties and statistics read from sysfs |
| `raw.resolv_conf` | Parsed resolver endpoints from `/etc/resolv.conf` |

The `raw` keys identify data sources. On Linux, `sysfs`, `iproute` and
`resolv_conf` coexist. Without the `ip` command, `raw.iproute.available` is
false and its `ipv4`/`ipv6` sections are null. Missing sysfs or resolver files
leave `raw.sysfs.interfaces` or `raw.resolv_conf` null; successful empty reads
produce an empty mapping or list. Unsupported platforms return an empty `raw`.
The primary-exit summary uses native routes and addresses from `raw.iproute`.

For example, an observed address has this JSON representation:

```json
{
  "interface": "eth0",
  "ifindex": 7,
  "address": "192.0.2.10",
  "prefixlen": 24
}
```

MAC addresses use lowercase colon notation, MTU is in bytes, and transmit link
speed is in bits per second. State uses `up`, `down`, `testing`, `unknown`,
`dormant`, `notpresent` or `lowerlayerdown`; a missing value is `null`.
A per-family MTU difference without a known link MTU leaves common `mtu`
null.

Route destinations always use CIDR notation, including `0.0.0.0/0` and `::/0`
for default routes and `/32` or `/128` for host routes. An on-link route has no
gateway. Linux multipath routes keep a `nexthops` list, each with `interface`,
`ifindex`, `gateway`, and `weight`; ordinary routes have an empty list. A route
without a single outgoing interface has `interface` and `ifindex` set to null.
`metric` is the route metric. These records describe observations, not a portable
routing-policy configuration. Linux route types, tables, rules, advanced
next-hop attributes and all original iproute2 fields remain in `raw.iproute`.
Routes from different tables may have identical common fields.

`default.ipv4` and `default.ipv6` always exist; each is null when no usable
default route is observed. Linux considers unicast defaults in the main table,
ordered by route metric. Down or missing
interfaces are excluded. Ties use interface index and gateway; multipath chooses
one representative next hop while retaining all hops in `routes`.

The selected interface's address prefers Linux `prefsrc`, then a non-link-local
usable address, then numeric address order. Tentative, duplicate and deprecated
addresses are excluded. If no usable
address is observed, `address` and `prefixlen` are null while the route remains.
This is a summary of the main exit, not a destination-specific routing lookup:
policy rules, ECMP hashing and source-address policy can produce another result.

Linux DNS endpoints come from `/etc/resolv.conf` and have null interface
fields. They can be local stubs, such as `127.0.0.53`; they are **not** assumed
to be upstream servers. The list does not describe split-DNS policy or DNS
selection order. For the Linux
service view, use the independent `systemd_resolved` and `networkd` branches.

Unavailable sections are `null`; a successful query with no entries gives
`[]` or `{}`. The `ipv4` and `ipv6` objects always exist, even when their
`addresses` and `routes` are null. Linux without `ip` still provides available
sysfs and resolver facts. Errors and timeouts from available commands
propagate rather than being cached as an empty network.

Linux queries observe the RPC process's network namespace and mounted sysfs.
Separate queries are not an atomic snapshot. Each command has a 30-second
timeout. The remote needs only Python's standard library and the OS commands.

## Processors and memory

The `cpu` branch reports the model, the architecture, how many processors the
machine has and how many this process may use, the topology, the caches and
the state of every hardware mitigation. The `memory` branch reports what the
kernel manages, what is free, what is available and the swap.

Every field of both branches may be absent, because a target can hide its
source or refuse a read. Name the gap instead of substituting a zero:

<!-- name: test_facts_capacity_report -->
```python
import asyncio

from rmote.tools import facts
from rmote.tools.facts.collectors.cpu import CpuInfo
from rmote.tools.facts.collectors.memory import MemoryInfo


def report_capacity(snapshot):
    """Describe the processor and the memory of one host, naming every gap."""
    cpu, memory = snapshot["cpu"], snapshot["memory"]
    threads = cpu.topology.logical if cpu.topology else None
    parts = [f"{cpu.model or 'unknown processor'} ({cpu.architecture})"]
    parts.append(f"{threads} threads" if threads else "thread count unknown")
    if memory.total_bytes is None:
        parts.append("memory unknown")
    else:
        parts.append(f"{memory.total_bytes / 2**30:.1f} GiB total")
        if memory.available_bytes is None:
            parts.append("available memory unknown")
        else:
            parts.append(f"{memory.available_bytes / 2**30:.1f} GiB available")
    return ", ".join(parts)


# The machine running this example, whatever it exposes.
print(report_capacity(asyncio.run(facts.gather(sections=["cpu", "memory"]))))

# A target that exposes nothing still gives a readable report.
nothing = {"cpu": CpuInfo(available=False, architecture="aarch64"), "memory": MemoryInfo(available=False)}
assert report_capacity(nothing) == "unknown processor (aarch64), thread count unknown, memory unknown"

# A target that reports its memory but hides its topology.
partial = {
    "cpu": CpuInfo(available=True, architecture="x86_64", model="Example CPU"),
    "memory": MemoryInfo(available=True, total_bytes=8 * 2**30),
}
assert report_capacity(partial) == (
    "Example CPU (x86_64), thread count unknown, 8.0 GiB total, available memory unknown"
)
```

The installed count and the usable count are separate. `topology.logical`
counts the online processors of the machine, and `usable` counts those this
process may run on, which a CPU affinity or a control group reduces. `quota`
expresses a control group CPU limit as a number of processors, so a container
limited to half a processor reports `0.5`.

`/proc/cpuinfo` repeats almost every field for each processor, so only the
first block describes the model, and the counts come from
`/sys/devices/system/cpu`. Sockets and cores per socket come from the
identifiers of `/proc/cpuinfo`; an architecture that omits them leaves those
counts null instead of a guess. `vulnerabilities` maps each mitigation the
kernel knows to its current state, such as `Not affected` or
`Mitigation: Enhanced IBRS`. A target without the cpufreq driver, such as
most virtual machines, reports only the current frequency.

Memory values are bytes, converted from the kibibytes of `/proc/meminfo`. A
target without that file reports `total_bytes` from `sysconf`, which counts the
same pages, and `free_bytes` when the system names the free ones; macOS has no
such name and leaves it null.
`total_bytes` is what the kernel manages, which is slightly below the
installed hardware because firmware and the kernel reserve some of it.
`available_bytes` is the kernel's own estimate of what a new workload can
take without swapping, and it exceeds `free_bytes` because reclaimable caches
count towards it. `used_bytes` is total minus available, not total minus free,
so a cache does not look like consumption. `limit_bytes` and `usage_bytes`
come from control-group files, alongside the totals of the machine. The
complete `/proc/meminfo` stays in `raw`.

### Control-group limits

The internal `rmote.tools.facts.collectors.cgroup` helper reads **cgroup v2
only**. It finds the collecting process's group from the `0::<path>` entry
in `/proc/self/cgroup`, relative to `/sys/fs/cgroup`. On a cgroup v1-only
host, or when that entry cannot be read, `cpu.quota`, `memory.limit_bytes`
and `memory.usage_bytes` are `None`.

A limit reports the strictest group, because the kernel enforces the limit of
every ancestor. The helper starts at the process's group, walks towards
`/sys/fs/cgroup`, including the root, and compares every readable file it
finds. A file that says `max`, is missing, is unreadable or holds malformed
text declares no limit and is skipped, so a child with `memory.max=max` still
reports the finite limit of its parent. `None` means that no group of the
chain limits the process.

`memory.limit_bytes` compares `memory.max`. `cpu.quota` compares `cpu.max`,
whose values are a quota and a period: each pair becomes a number of
processors before the comparison, because the periods of two groups can
differ. A child with `200000 100000` nested in a parent with `100000 1000000`
reports `0.1`, not `2.0`, although the parent names the larger quota.

`memory.usage_bytes` reads `memory.current` in the process's own group and
never in a parent, because a parent file counts the processes of its other
children too. It is `None` when that group has no readable usage file.

The walk ends at the visible mount root. Inside a cgroup namespace,
`/proc/self/cgroup` names the group relative to the namespace root, so a
limit set above that boundary is enforced but cannot be read. `None`
therefore means "no visible limit" and does not prove that the process is
unrestricted.

## Storage, mounts and free space

The `storage` branch describes mounted filesystems with their space, block
devices and swap. Every source is a kernel file or a standard library call, so
no command runs and no privileges are needed.

```python
def report_free_space(snapshot):
    """Print the space a caller can still use on every real filesystem."""
    for mount in snapshot["storage"].mounts or ():
        if mount.usage and not mount.pseudo:
            free = mount.usage.available_bytes / 2**30
            print(f"{mount.target:<20}{mount.fstype:<8}{free:.1f} GiB free")
```

Mounts come from `/proc/self/mountinfo`, which also gives the device number,
the mount root and the shadowing order. The older `/proc/mounts` is the
fallback and reports neither a device number nor a root. Octal escapes in a
path, such as `\040` for a space, are decoded.

Mounts keep the order of the kernel, so a mount that shadows an earlier one at
the same target stays visible. Nothing is filtered: a kernel filesystem that
stores no data carries `pseudo`, and the caller selects what it needs.

Space comes from `os.statvfs` for each real filesystem, in bytes.
`free_bytes` is what the filesystem reports as unused, and `available_bytes`
is what an unprivileged process can still use; the two differ when the
filesystem reserves space for the superuser. `usage` is null for a pseudo
filesystem, which is not queried, or when `os.statvfs` raises `OSError`, such
as a permission error. A filesystem without inodes reports null inode counts.

Block devices come from `/sys/block`. Sizes are bytes, converted from the
512-byte sectors that sysfs always reports, whatever the hardware block size.
Each whole device lists the sizes of its partitions. Swap areas come from
`/proc/swaps`, converted from kibibytes to bytes. The configured table of
`/etc/fstab` is reported without interpretation.

`available` reports whether the mount table could be read. Each section is
null when its source is absent, which separates an unsupported target from an
empty but successful read. One unreadable source leaves its own section null
and keeps the others. `raw` keeps the text of every source that was read.

`root` is the space of the root filesystem and is filled whatever `available`
says, because `os.statvfs` answers on every POSIX target while the mount table
does not. On Linux the same numbers are the usage of the `/` mount. A target
without the mount table reports `root` alone: no portable call lists the
mounts, so `mounts` stays null there.

`os.statvfs` on an unresponsive mount can block indefinitely. The collector has
no per-mount timeout: a blocked call does not produce a result with `usage=None`.
It runs in a worker thread, so other collectors can progress, but `fetch` and
`gather` wait for all selected branches before returning a snapshot.

`storage` is included in `DEFAULT_COLLECTORS`. If a collection must avoid
waiting on mounts, select other branches explicitly, for example
`sections=["system", "python"]`, or supply a collector set without
`StorageFacts`. Cancelling an RPC only stops local waiting; it does not
interrupt a remote `statvfs` call already in progress.

## Choose a cache and reuse results

Collection, freshness and persistence are explicit operations. Load saved entries,
check which sections need refreshing, then open a connection only if needed:

<!-- name: test_facts_guide; fixtures: docs_ssh; mark: timeout(30) -->
```python
import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from typing import Any
from rmote.cache import Cache, SQLiteCache
from rmote.protocol import Protocol
from rmote.tools import facts


async def main(directory):
    backend = SQLiteCache(Path(directory) / "facts.sqlite")
    cache = Cache[Any](versions={c.key: c.version for c in facts.DEFAULT_COLLECTORS})
    host = "user@server"
    keys = ["system", "python"]
    await cache.load(backend, namespace=host, keys=keys)
    missing = cache.stale(keys=keys, max_age=300)
    if missing:
        async with await Protocol.from_ssh(host) as remote:
            started = time.time()
            cache.update(
                await facts.fetch(remote, sections=missing, concurrency=4),
                collected_at=started,
            )
        await cache.save(backend, namespace=host, keys=missing)
    snapshot = facts.validate(cache.data)
    assert set(snapshot) == set(keys)
    assert snapshot["system"].hostname
    assert snapshot["python"].version
    assert not cache.stale(keys=keys)

    # Read persisted facts without opening a connection.
    saved = Cache[Any]()
    await saved.load(backend, namespace=host, keys=keys)
    assert saved.data == snapshot
    saved.invalidate(keys=["python"])
    assert set(saved.data) == {"system"}
    # Invalidation above affects memory only. Durable deletion is explicit.
    await backend.delete(host, "python")


with TemporaryDirectory() as directory:
    asyncio.run(main(directory))
```

For an in-memory cache alone, the entire operation is:

```python
cache = Cache[Any]()
cache.update(await facts.fetch(remote))
```

`update` is synchronous and replaces supplied sections in memory, preserving
others. The cache retains values as supplied, without copying or freezing them.
`cache.data` is a read-only key mapping sharing those values. Their owner is
responsible for mutations. Fact model validation belongs to `fetch`, `gather`
and the explicit `facts.validate(cache.data)` helper. By default,
timestamps come from the controller at update time. Supply `collected_at` with
the collection start time to order overlapping observations. Older timestamps
cannot overwrite newer ones in memory or in the bundled backends.

`stale(max_age=300)` identifies sections older than five minutes. Zero requests
all selected sections; None accepts any age. Missing sections and schema-version
mismatches always require collection. `load` merges saved entries regardless of
age, omitting incompatible versions and retaining newer in-memory observations.
A failed load leaves the existing data unchanged.

`Cache[Any]()` performs no I/O and chooses no backend. Use `JSONCacheDir(path)`
or `SQLiteCache(path)` explicitly. SQLite's parent directory must already exist.
JSON documents use percent-encoded host names, such as `admin@server.json`,
and one-space indentation. Both backends support multiple controller processes
and atomic replacement of the selected sections as a batch. JSON reads and
replaces the host document once per save; SQLite uses one transaction. A failed
batch does not leave earlier sections persisted. Newer timestamps are preserved
independently for each key, and omitted keys are not deleted. Loading selected
sections reads one consistent backend snapshot.

Custom backends implement `CacheBackend.get_many(namespace, keys)` and
`set_many(namespace, entries)` as well as single-key `get`, `set`, and `delete`.
`get_many` returns a dictionary of entries, omitting missing keys. `set_many`
validates and merges the entire batch atomically. Serialization and blocking
storage work belong in the backend's worker thread. Cancelling an await does
not stop an already-running worker; its batch may still commit.

Use one Cache per host and execution context. Its first load/save binds its
namespace; another namespace requires a separate instance. Cache freshness never
opens a connection or initiates collection automatically.

Both helpers perform independent observations, not an atomic snapshot of the
host. If any collector fails, neither returns a result, so
`cache.update(await facts.fetch(remote))` leaves the entire cache unchanged.
Unfinished siblings are cancelled and awaited; already running synchronous work
cannot be stopped. Cancelling `fetch` stops local waiting only, as described in
{doc}`../../concepts`. Call a collector on its own when a failing branch must not
affect the others. Concurrent collections are independent, without implicit
coalescing.

## Add a collector

Define a `Tool` class in a readable module, as described in
{doc}`../../writing-tools`. Give it a unique `key`, a positive integer `version` and
a `result_type` dataclass and a static `collect()` method returning that model. Increment
`version` when the cached structure or meaning changes. Helper methods belong
inside the collector class; use `async def` and `await async_process(...)` for
command queries.

Pass collector classes in `await facts.fetch(remote, collectors=[...])` and
configure the local `Cache[Any](versions={c.key: c.version for c in [...]})` with the same definitions. This
replaces the default collector set. Use `sections=[...]` to choose registered
keys. Tool classes passed as arguments carry their source definitions using the
usual class Tool transfer rules; the cache stays on the controller.

## Typed results and cache representations

Each collector declares its result dataclass in the same module as the Tool.
`fetch()`, `gather()` and `facts.validate(cache.data)` return dictionaries described by
`FactsData(TypedDict, total=False)`. Literal string keys retain their model types:
`snapshot["network"]` is `NetworkInfo`, and `snapshot["system"]` is `SystemInfo`.
Any key may be absent after selecting sections or reading an incomplete cache;
check membership or use `.get()` when the key's presence is uncertain.

Nested common records are dataclasses too, for example
`snapshot["network"].default.ipv4`. Check optional fields for `None` before
accessing their properties. Native `raw`, systemd properties and variable
vendor records remain dictionaries.

Direct remote calls preserve the same model type: `await remote(NetworkFacts.collect)`
with `Protocol`, or `remote(NetworkFacts.collect)` with `Connection`, returns
`NetworkInfo`. A helper accepting `collector: Collector[T]` keeps `T` through
either client. Cached and offline results
use the same dataclass types as fresh collections.

To export ordinary JSON or pass facts into a template context:

```python
from dataclasses import asdict
from typing import cast
from rmote.serialization import Dataclass

plain = {key: asdict(cast(Dataclass, value)) for key, value in snapshot.items()}
```

A custom collector can declare its model alongside the Tool:

```python
from dataclasses import dataclass
from rmote.protocol import Tool

@dataclass
class ExampleInfo:
    enabled: bool

class ExampleFacts(Tool):
    key = "example"
    version = 1
    result_type = ExampleInfo

    @staticmethod
    def collect() -> ExampleInfo:
        return ExampleInfo(enabled=True)
```

Extend `FactsData` to describe custom collector keys. The collector registry
is dynamic, so a type checker cannot infer those keys from `collectors=[...]`.
Declare the schema at the application boundary with `cast`; it does not convert
or copy the dictionary:

```python
from typing import cast
from typing import Any
from rmote.cache import Cache
from rmote.tools.facts.schema import FactsData
from rmote.tools import facts

class ProjectFacts(FactsData, total=False):
    example: ExampleInfo

async def collect_project(remote):
    cache = Cache[Any](versions={c.key: c.version for c in [ExampleFacts]})
    snapshot = cast(ProjectFacts, await facts.fetch(remote, collectors=[ExampleFacts]))
    cache.update(snapshot)
    assert snapshot["example"].enabled
    return cast(ProjectFacts, facts.validate(cache.data, collectors=[ExampleFacts]))
```

`total=False` makes keys optional. Additional keys are declared by extending the
schema; unknown literal keys and incompatible model assignments remain type
errors. Built-in keys retain their declared model types even when supplied by
custom collectors. For dynamic iteration, `TypedDict` values are typed as
`object`, hence the cast in the JSON-export example above.

`CacheEntry[T]` carries the model, collection time and collector schema version.
A backend accepts and returns entries containing models. `Cache` never serializes
JSON: custom memory, pickle or database backends can use other representations.
The bundled backends use `json_default()` and `json_object_hook()` to preserve
class identities through JSON, recursively including nested models. The
`dump_dataclass()` and `load_dataclass()` helpers also expose a materialized
dictionary representation when needed:

```python
from rmote.serialization import dump_dataclass, load_dataclass
from rmote.tools.facts.collectors.network import NetworkInfo

encoded = dump_dataclass(network)
restored = load_dataclass(encoded, NetworkInfo)
```

The record contains `"_type": "dataclass"` and an `_value` object with `class`
(`"module:qualname"`) and `fields`. Bytes use `"_type": "bytes"` and a base64
string in `_value`. The exact two-key shape `{ "_type": ..., "_value": ... }`
is reserved and must not be used for ordinary data. Other values follow standard
JSON semantics, including conversion of tuples to lists. Unsupported objects,
cycles and function-local dataclasses are rejected.
`init=False` fields are recomputed by constructors. `asdict()` is intentionally
not used before cache encoding because it discards nested class identities.

Cache files are trusted local data: loading imports recorded modules and calls
constructors. Keep model paths importable across runs. Constructors provide their
normal validation; the helper does not validate every field against annotations.
Increment the collector version when changing its model and migrate or clear
entries if removing or renaming recorded classes. Malformed entries and I/O
errors propagate; they do not silently become misses.

`JSONCacheDir` uses POSIX file locks. `SQLiteCache` does not import `fcntl`.

(facts-operations-and-types)=
## Operations and types

The `rmote.tools.facts` package exports `fetch` for remote collection, `gather`
for collection on the calling side, and `validate` for checking existing results.
`fetch` accepts an asynchronous callable, so custom transports can use the same
entry point. `DEFAULT_COLLECTORS` supplies the default set; `Collector` and
`FactsData` describe the collector contract and result types.

```{eval-rst}
.. autofunction:: rmote.tools.facts.fetch

.. autofunction:: rmote.tools.facts.gather

.. autofunction:: rmote.tools.facts.validate

.. automodule:: rmote.tools.facts.schema
   :members: Collector, CollectorRegistry, FactsData, DEFAULT_COLLECTORS

.. automodule:: rmote.tools.facts.collectors.cpu
   :members: CpuFacts, CpuInfo, CpuTopology, CpuFrequency, CpuCache

.. automodule:: rmote.tools.facts.collectors.memory
   :members: MemoryFacts, MemoryInfo, SwapMemory, HugePages

.. automodule:: rmote.tools.facts.collectors.storage
   :members: StorageFacts, StorageInfo, MountPoint, FilesystemUsage, BlockDevice, SwapArea

.. automodule:: rmote.tools.facts.collectors.network
   :members: NetworkFacts, NetworkdFacts, NetworkInfo, NetworkInterface, NetworkAddress, NetworkNextHop, NetworkRoute, NetworkFamily, NetworkDefaults, DefaultRoute, DNSServer, NetworkdInfo

.. automodule:: rmote.tools.facts.collectors.system
   :members: SystemFacts, SystemInfo, DistributionInfo

.. automodule:: rmote.tools.facts.collectors.python
   :members: PythonFacts, PythonInfo

.. automodule:: rmote.tools.facts.collectors.apt
   :members: AptFacts, AptInfo, AptPackage

.. automodule:: rmote.tools.facts.collectors.pacman
   :members: PacmanFacts, PacmanInfo, PacmanPackage

.. automodule:: rmote.tools.facts.collectors.systemd
   :members: SystemdFacts, SystemdTimesyncFacts, SystemdResolvedFacts, SystemdInfo, TimesyncInfo, ResolvedInfo
```
