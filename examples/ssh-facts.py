#!/usr/bin/env python3
"""Print remote facts: uv run examples/ssh-facts.py hostname [hostname ...].

Each collector runs in its own RPC; the controller joins the branches.
Uses SSH configuration and authentication, the selected remote interpreter,
and JSONCacheDir at .cache/facts. Cached branches are reused for 60 seconds by
default; --max-age sets their lifetime and --max-age 0 forces a refresh.
SSH is opened only when a branch needs collecting.
Hosts are collected concurrently and printed as one JSON object keyed by hostname.
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from rmote.cache import Cache, JSONCacheDir
from rmote.protocol import Protocol
from rmote.serialization import Dataclass
from rmote.tools import facts


async def host_facts(hostname: str, python: str = "python3", max_age: int = 0) -> dict[str, Any]:
    """Collect every registered branch for one host."""
    backend = JSONCacheDir(Path(".cache") / "facts")
    collectors = facts.DEFAULT_COLLECTORS
    versions = {collector.key: collector.version for collector in collectors}
    cache = Cache[Any](versions=versions)
    await cache.load(backend, namespace=hostname, keys=versions)
    facts.validate(cache.data, collectors=collectors)
    sections = cache.stale(max_age=max_age)
    if sections:
        async with await Protocol.from_ssh(hostname, python=python) as remote:
            started = time.time()
            branches = await facts.fetch(
                remote,
                sections=sections,
                collectors=collectors,
            )
            cache.update(branches, collected_at=started)
        await cache.save(backend, namespace=hostname, keys=sections)
    return {hostname: {key: asdict(cast(Dataclass, value)) for key, value in cache.data.items()}}


async def amain(*hostname: str, python: str = "python3", max_age: int = 0) -> None:
    snapshots = await asyncio.gather(*(host_facts(h, python, max_age=max_age) for h in hostname))
    result = {}
    for snapshot in snapshots:
        result.update(snapshot)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect all remote facts over SSH and print JSON.")
    parser.add_argument("hostname", help="SSH hostname or user@hostname (uses your SSH config)", nargs="+")
    parser.add_argument("--python", default="python3", help="Remote Python executable")
    parser.add_argument(
        "--max-age", default=60, type=int, help="Maximum cache age in seconds (default: 60; 0 refreshes)"
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(amain(*arguments.hostname, python=arguments.python, max_age=arguments.max_age))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ssh-facts: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
