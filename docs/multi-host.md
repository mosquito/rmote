# Work with Multiple Hosts

This guide collects log filenames from several SSH hosts. It then adds a
concurrency limit, handles an unavailable host, and reuses open connections.
Replace the example hostnames with machines in your SSH configuration.
The same scheduling pattern applies to other {doc}`transports`; choose the
appropriate connection factory for each target.

## Collect Results with a Concurrency Limit

Use a semaphore to limit how many hosts are connected at once. Here at most
two connections are active, including their startup and cleanup:

<!-- name: test_multi_host; case: inventory; fixtures: docs_ssh; mark: timeout(30) -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


async def list_logs(host, limit):
    async with limit:
        async with await Protocol.from_ssh(host) as remote:
            files = await remote(FileSystem.glob, "/var/log", "*.log")
            return sorted(files)


async def inventory():
    limit = asyncio.Semaphore(2)
    results = await asyncio.gather(*[list_logs(host, limit) for host in HOSTS])
    assert len(results) == len(HOSTS)
    for host, files in zip(HOSTS, results):
        assert all(path.startswith("/var/log/") and path.endswith(".log") for path in files)
        print(f"{host}: {len(files)} log files")


asyncio.run(inventory())
```

`gather` returns results in the order of its input calls, even when hosts finish
in another order. Each connection closes after its operation completes.
An empty list means there were no matching files visible to that SSH user.

The semaphore limits active connections, but `gather` still schedules one task
per input host. For a large inventory, feed hosts to a fixed number of workers
through an `asyncio.Queue` instead of scheduling the whole inventory at once.

## Keep Results When One Host Fails

By default, `gather` propagates the first exception to its caller. Set
`return_exceptions=True` to wait for all calls and inspect each result.
Append this example to the script above:

<!-- name: test_multi_host; case: partial_failure -->
```python
async def inventory_with_errors():
    hosts = ["web1", "unreachable.example", "web3"]
    limit = asyncio.Semaphore(2)
    results = await asyncio.gather(
        *[list_logs(host, limit) for host in hosts],
        return_exceptions=True,
    )
    assert len(results) == len(hosts)
    assert isinstance(results[0], list) and isinstance(results[2], list)
    assert isinstance(results[1], Exception)
    for host, result in zip(hosts, results):
        if isinstance(result, BaseException):
            print(f"{host}: failed: {result}")
        else:
            print(f"{host}: {len(result)} log files")


asyncio.run(inventory_with_errors())
```

`unreachable.example` intentionally represents an unavailable host. Replace it
with a known unreachable target when trying this failure case. The assertions
expect the other two hosts to work.

`return_exceptions=True` changes how errors are reported; it does not make
operations succeed or retry them. Cancellation of the enclosing `gather` is
still cancellation, not a normal per-host error result.

## Reuse Connections for Related Operations

For several operations on each host, keep the connections in an
`AsyncExitStack`. This example lists logs and reads host mappings over the
same connections:

<!-- name: test_multi_host; case: reuse_connections -->
```python
from contextlib import AsyncExitStack


async def inspect_hosts():
    async with AsyncExitStack() as stack:
        connections = [
            await stack.enter_async_context(await Protocol.from_ssh(host))
            for host in HOSTS
        ]
        logs = await asyncio.gather(*[
            remote(FileSystem.glob, "/var/log", "*.log")
            for remote in connections
        ])
        mappings = await asyncio.gather(*[
            remote(FileSystem.read_str, "/etc/hosts")
            for remote in connections
        ])
        assert len(logs) == len(mappings) == len(HOSTS)
        for host, files, content in zip(HOSTS, logs, mappings):
            assert "localhost" in content
            print(f"{host}: {len(files)} logs; {len(content.splitlines())} host-file lines")


asyncio.run(inspect_hosts())
```

Connections open sequentially here. If opening a later host fails, the stack
closes those already opened. Calls then run concurrently across the open
connections, and the stack closes them when the block exits.

For a periodic check, repeat the operation while keeping the stack open.
Handle transport failures by closing and replacing the failed connection.
rmote does not reconnect automatically. Decide which operations are safe to
retry: a lost reply does not prove that the remote operation did not happen.

## Use a Synchronous Client

In a synchronous program, use a thread pool. Each worker owns its connection;
`max_workers` bounds the number of simultaneous connections:

<!-- name: test_multi_host_sync; fixtures: docs_ssh; mark: timeout(30) -->
```python
from concurrent.futures import ThreadPoolExecutor
from rmote.sync import Connection
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


def list_logs(host):
    with Connection.from_ssh(host, rpc_timeout=10.0) as remote:
        return host, sorted(remote(FileSystem.glob, "/var/log", "*.log"))


with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(list_logs, HOSTS))

assert [host for host, files in results] == HOSTS
for host, files in results:
    assert all(path.startswith("/var/log/") and path.endswith(".log") for path in files)
    print(f"{host}: {len(files)} log files")
```

Catch expected exceptions inside a worker if you need one result per host after
a failure. The connection context still closes when a worker raises.

An open `Connection` can also serve several caller threads. Keep it open until
those callers finish. Each connection owns a background loop thread; there is
no shared runtime. See {doc}`api/sync` for deadlines and cleanup.
