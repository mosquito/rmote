"""Measure cache overhead without remote collection or network I/O.

Run ``uv run python -m benchmarks.facts_cache --profile-dir /tmp/cache-profiles``.
Synthetic package inventories use real fact models. The total number of packages
stays fixed when split across sections. Timings are warmed medians, without a
profiler attached. CPU includes worker threads; event-loop lag is measured in a
separate run with a 1 ms heartbeat. The disk is shared with everything else on
the machine, so parallel load inflates the absolute numbers; compare the rows of
one run. Profiles of synchronous backend methods also
capture worker work that cProfile on the event-loop thread would miss.
"""

import argparse
import asyncio
import copy
import cProfile
import json
import platform
import statistics
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rmote.cache import Cache, JSONCacheDir, SQLiteCache
from rmote.cache.backends import CacheBackend, JSONEntryCodec
from rmote.serialization import dump_dataclass, load_dataclass
from rmote.tools.facts.collectors.apt import AptInfo, AptPackage


def workload(packages: int, sections: int) -> tuple[Cache[AptInfo], dict[str, AptInfo]]:
    values = {
        f"section-{section}": AptInfo(
            available=True,
            version="apt 2.6.1 (amd64)",
            packages={
                f"package-{index:06d}": AptPackage(f"1.0.{index}", "amd64", "install ok installed")
                for index in range(section, packages, sections)
            },
        )
        for section in range(sections)
    }
    cache = Cache[AptInfo](versions=dict.fromkeys(values, 1))
    cache.update(values, collected_at=1)
    return cache, values


def summary(samples: list[tuple[float, float]]) -> dict[str, float]:
    return {
        "wall_ms": round(statistics.median(wall for wall, _ in samples) * 1000, 3),
        "cpu_ms": round(statistics.median(cpu for _, cpu in samples) * 1000, 3),
    }


def measure(operation: Callable[[], object], repeats: int) -> dict[str, float]:
    operation()
    samples = []
    for _ in range(repeats):
        wall, cpu = time.perf_counter(), time.process_time()
        operation()
        samples.append((time.perf_counter() - wall, time.process_time() - cpu))
    return summary(samples)


async def measure_async(operation: Callable[[], Awaitable[object]], repeats: int) -> dict[str, float]:
    await operation()
    samples = []
    for _ in range(repeats):
        wall, cpu = time.perf_counter(), time.process_time()
        await operation()
        samples.append((time.perf_counter() - wall, time.process_time() - cpu))
    return summary(samples)


async def loop_lag(operation: Callable[[], Awaitable[object]]) -> float:
    lags = []
    stop = False

    async def heartbeat() -> None:
        while not stop:
            start = time.perf_counter()
            await asyncio.sleep(0.001)
            lags.append(max(0, time.perf_counter() - start - 0.001))

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        await operation()
    finally:
        stop = True
        await task
    return round(max(lags, default=0) * 1000, 3)


def profile(path: Path, operation: Callable[[], object]) -> None:
    profiler = cProfile.Profile()
    profiler.runcall(operation)
    profiler.dump_stats(str(path))


async def scenario(packages: int, sections: int, repeats: int, profile_dir: Path | None) -> dict[str, Any]:
    cache, values = workload(packages, sections)
    encoded = {key: dump_dataclass(value) for key, value in values.items()}
    payloads = {key: JSONEntryCodec.dumps(entry) for key, entry in cache.entries.items()}
    operations: dict[str, Callable[[], object]] = {
        "update": lambda: cache.update(values, collected_at=1),
        "data": lambda: cache.data,
        "stale": lambda: cache.stale(max_age=None),
        "deepcopy": lambda: copy.deepcopy(values),
        "dump_dataclass": lambda: [dump_dataclass(value) for value in values.values()],
        "load_dataclass": lambda: [load_dataclass(value) for value in encoded.values()],
        "json_dumps": lambda: json.dumps(encoded, allow_nan=False),
        "codec_dumps": lambda: [JSONEntryCodec.dumps(entry) for entry in cache.entries.values()],
        "codec_loads": lambda: [JSONEntryCodec.loads(payload) for payload in payloads.values()],
    }
    result: dict[str, Any] = {
        "packages": packages,
        "sections": sections,
        "payload_bytes": sum(len(payload.encode()) for payload in payloads.values()),
        "memory": {name: measure(operation, repeats) for name, operation in operations.items()},
    }
    prefix = f"{packages}-{sections}"
    if profile_dir is not None:
        for name, operation in operations.items():
            profile(profile_dir / f"{prefix}-{name}.prof", operation)
    with TemporaryDirectory(prefix="rmote-cache-bench-") as directory:
        root = Path(directory)
        backends: dict[str, CacheBackend] = {
            "json": JSONCacheDir(root / "json"),
            "sqlite": SQLiteCache(root / "cache.sqlite"),
        }
        for name, backend in backends.items():
            await cache.save(backend, namespace="benchmark")
            loaded, _ = workload(packages, sections)
            loaded.invalidate()
            await loaded.load(backend, namespace="benchmark")
            assert loaded.data == values

            async def save(target: CacheBackend = backend) -> None:
                await cache.save(target, namespace="benchmark")

            async def load(target: CacheBackend = backend) -> None:
                await cache.load(target, namespace="benchmark")

            for label, action in (("save", save), ("load", load)):
                metrics = await measure_async(action, repeats)
                metrics["max_loop_lag_ms"] = await loop_lag(action)
                result[f"{name}_{label}"] = metrics

            # One branch of an existing document: the merge and the read must
            # not pay for the branches they do not touch.
            first = dict(list(cache.entries.items())[:1])
            assert isinstance(backend, (JSONCacheDir, SQLiteCache))
            store, keys = backend, tuple(first)

            def write_one(target: Any = store, batch: Any = first) -> None:
                target.write_entries("benchmark", batch)

            def read_one(target: Any = store, selected: Any = keys) -> None:
                target.read_entries("benchmark", selected)

            result[f"{name}_write_one"] = measure(write_one, repeats)
            result[f"{name}_read_one"] = measure(read_one, repeats)
            if profile_dir is not None:
                profiler = cProfile.Profile()
                profiler.enable()
                await save()
                await load()
                profiler.disable()
                profiler.dump_stats(str(profile_dir / f"{prefix}-{name}-controller.prof"))

                def write_batch(target: CacheBackend = backend) -> None:
                    assert isinstance(target, (JSONCacheDir, SQLiteCache))
                    target.write_entries("benchmark", cache.entries)

                def read_batch(target: CacheBackend = backend) -> None:
                    assert isinstance(target, (JSONCacheDir, SQLiteCache))
                    target.read_entries("benchmark", values)

                profile(profile_dir / f"{prefix}-{name}-worker-write.prof", write_batch)
                profile(profile_dir / f"{prefix}-{name}-worker-read.prof", read_batch)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--sections", type=int, nargs="+", default=[1, 9])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile-dir", type=Path)
    args = parser.parse_args()
    if min([*args.packages, *args.sections, args.repeats]) < 1:
        parser.error("counts must be positive")
    if args.profile_dir is not None:
        args.profile_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(), "repeats": args.repeats}))
    for packages in args.packages:
        for sections in args.sections:
            print(json.dumps(await scenario(packages, sections, args.repeats, args.profile_dir)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
