# Multi-Host Operations

All patterns below connect to three hosts in parallel using
{meth}`~rmote.protocol.BaseProtocol.from_ssh` and `asyncio.gather`.

## Fan-out: same command on every host

Open a connection, run one call, close.  All three connections are established
and executed concurrently:

<!-- name: test_fanout -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


async def read_hostname(host: str) -> str:
    async with await Protocol.from_ssh(host) as proto:
        return await proto(FileSystem.read_str, "/etc/hostname")


async def main() -> None:
    names = await asyncio.gather(*[read_hostname(h) for h in HOSTS])
    for host, name in zip(HOSTS, names):
        print(f"{host}: {name.strip()}")


if __name__ == "__main__":
    asyncio.run(main())
```

Each connection is bootstrapped, synced, and executed in parallel.
`asyncio.gather` at the outer level fans out across all three hosts simultaneously:

```{mermaid}
sequenceDiagram
    participant L as Local
    participant W1 as web1
    participant W2 as web2
    participant W3 as web3

    par open & run
        L->>W1: bootstrap + SYNC FileSystem
        L->>W2: bootstrap + SYNC FileSystem
        L->>W3: bootstrap + SYNC FileSystem
    end
    par gather results
        W1-->>L: read_str → "web1"
        W2-->>L: read_str → "web2"
        W3-->>L: read_str → "web3"
    end
```

## Multiple commands per host

Run several calls on each host while the connection is open.
The inner `asyncio.gather` pipelines all calls over the same connection:

<!-- name: test_multi_commands -->
```python
import asyncio
import dataclasses
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


@dataclasses.dataclass
class HostInfo:
    host: str
    hostname: str
    uptime: str
    load: str


async def collect(host: str) -> HostInfo:
    async with await Protocol.from_ssh(host) as proto:
        hostname, uptime, load = await asyncio.gather(
            proto(FileSystem.read_str, "/etc/hostname"),
            proto(FileSystem.read_str, "/proc/uptime"),
            proto(FileSystem.read_str, "/proc/loadavg"),
        )
    return HostInfo(
        host=host,
        hostname=hostname.strip(),
        uptime=uptime.split()[0] + "s",
        load=load.split()[0],
    )


async def main() -> None:
    results = await asyncio.gather(*[collect(h) for h in HOSTS])
    for info in results:
        print(f"{info.host}  hostname={info.hostname}  uptime={info.uptime}  load={info.load}")


if __name__ == "__main__":
    asyncio.run(main())
```

The outer `gather` parallelises across hosts; the inner `gather` pipelines all three calls
over the same already-open connection without waiting for any individual response:

```{mermaid}
sequenceDiagram
    participant L as Local
    participant W1 as web1
    participant W2 as web2
    participant W3 as web3

    par per host: open connection
        L->>W1: bootstrap + SYNC FileSystem
        L->>W2: bootstrap + SYNC FileSystem
        L->>W3: bootstrap + SYNC FileSystem
    end
    par per host: three concurrent calls
        L->>W1: REQUEST hostname id=1
        L->>W1: REQUEST uptime   id=2
        L->>W1: REQUEST loadavg  id=3
        L->>W2: REQUEST hostname id=1
        L->>W2: REQUEST uptime   id=2
        L->>W2: REQUEST loadavg  id=3
        L->>W3: REQUEST hostname id=1
        L->>W3: REQUEST uptime   id=2
        L->>W3: REQUEST loadavg  id=3
        W1-->>L: hostname id=1
        W1-->>L: uptime   id=2
        W1-->>L: loadavg  id=3
        W2-->>L: hostname id=1
        W2-->>L: uptime   id=2
        W2-->>L: loadavg  id=3
        W3-->>L: hostname id=1
        W3-->>L: uptime   id=2
        W3-->>L: loadavg  id=3
    end
```

## Error isolation per host

Pass `return_exceptions=True` to `asyncio.gather` so a failure on one host
does not cancel the others:

<!-- name: test_error_isolation -->
```python
import asyncio
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "broken-host"]


async def read_hostname(host: str) -> str:
    async with await Protocol.from_ssh(host) as proto:
        return await proto(FileSystem.read_str, "/etc/hostname")


async def main() -> None:
    results = await asyncio.gather(
        *[read_hostname(h) for h in HOSTS],
        return_exceptions=True,
    )
    for host, result in zip(HOSTS, results):
        if isinstance(result, BaseException):
            print(f"{host}: ERROR - {result}")
        else:
            print(f"{host}: {result.strip()}")


if __name__ == "__main__":
    asyncio.run(main())
```

With `return_exceptions=True`, a failure on one host is captured as an exception value rather than
propagated immediately. The other calls continue to completion:

```{mermaid}
sequenceDiagram
    participant L as Local
    participant W1 as web1
    participant W2 as web2
    participant B  as broken-host

    par
        L->>W1: bootstrap + SYNC
        L->>W2: bootstrap + SYNC
        L->>B:  bootstrap + SYNC
    end
    par
        W1-->>L: read_str → "web1"
        W2-->>L: read_str → "web2"
        B-->>L:  EXCEPTION ConnectionRefusedError
    end
    Note over L: result[0] = "web1"
    Note over L: result[1] = "web2"
    Note over L: result[2] = ConnectionRefusedError(...)
```

## Keep connections open across multiple rounds

Open all connections first, then issue batches of commands without reconnecting:

<!-- name: test_persistent_connections -->
```python
import asyncio
from contextlib import AsyncExitStack
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]


async def main() -> None:
    async with AsyncExitStack() as stack:
        protos = await asyncio.gather(*[
            stack.enter_async_context(await Protocol.from_ssh(h))
            for h in HOSTS
        ])

        # Round 1 - read hostnames
        names = await asyncio.gather(*[
            p(FileSystem.read_str, "/etc/hostname") for p in protos
        ])
        print("hostnames:", [n.strip() for n in names])

        # Round 2 - list log files
        logs = await asyncio.gather(*[
            p(FileSystem.glob, "/var/log", "*.log") for p in protos
        ])
        for host, filelist in zip(HOSTS, logs):
            print(f"{host}: {len(filelist)} log files")


if __name__ == "__main__":
    asyncio.run(main())
```

Connections are established once and reused across rounds.
Each round fans out across all open connections simultaneously:

```{mermaid}
sequenceDiagram
    participant L as Local
    participant W1 as web1
    participant W2 as web2
    participant W3 as web3

    Note over L,W3: Open all connections
    par
        L->>W1: bootstrap + SYNC FileSystem
        L->>W2: bootstrap + SYNC FileSystem
        L->>W3: bootstrap + SYNC FileSystem
    end

    Note over L,W3: Round 1 - read hostnames
    par
        L->>W1: REQUEST read_str "/etc/hostname"
        L->>W2: REQUEST read_str "/etc/hostname"
        L->>W3: REQUEST read_str "/etc/hostname"
        W1-->>L: "web1"
        W2-->>L: "web2"
        W3-->>L: "web3"
    end

    Note over L,W3: Round 2 - list log files (same connections)
    par
        L->>W1: REQUEST glob "/var/log" "*.log"
        L->>W2: REQUEST glob "/var/log" "*.log"
        L->>W3: REQUEST glob "/var/log" "*.log"
        W1-->>L: [...]
        W2-->>L: [...]
        W3-->>L: [...]
    end
```

(persistent-daemon)=
## Persistent configuration daemon

Tools like Ansible open connections, push their modules, and exit. Every run pays the
bootstrap cost again — SSH handshake, Python startup, module transfer. Running Ansible every
30 seconds on 50 hosts means thousands of short-lived SSH connections per day.

With `rmote` the `Protocol` is just an object; you decide when to close it. Bootstrap happens
once. Every subsequent RPC call travels over the already-open channel at the cost of a single
round-trip packet. This makes it trivial to write a long-running daemon that continuously
enforces configuration state:

<!-- name: test_config_daemon -->
```python
import asyncio
from contextlib import AsyncExitStack
from rmote.protocol import Protocol
from rmote.tools import FileSystem

HOSTS = ["web1", "web2", "web3"]
WANTED = "PasswordAuthentication no\nPermitRootLogin no\n"
CONFIG = "/etc/ssh/sshd_config.d/hardening.conf"


async def enforce(proto: Protocol, host: str) -> None:
    try:
        current = await proto(FileSystem.read_str, CONFIG)
    except FileNotFoundError:
        current = ""
    if current != WANTED:
        await proto(FileSystem.write, CONFIG, WANTED)
        print(f"{host}: drift corrected")


async def daemon() -> None:
    async with AsyncExitStack() as stack:
        protos = await asyncio.gather(*[
            stack.enter_async_context(await Protocol.from_ssh(h))
            for h in HOSTS
        ])
        while True:
            await asyncio.gather(*[enforce(p, h) for p, h in zip(protos, HOSTS)])
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(daemon())
```

The `while True` loop runs forever on the already-open connections. Thirty seconds of idle
time costs nothing — the remote Python process simply blocks waiting for the next packet.
There is no reconnect, no re-bootstrap, no re-sync.
