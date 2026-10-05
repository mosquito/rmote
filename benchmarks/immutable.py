"""Compare initial freezing, snapshot reuse and replacement of one branch.

Run ``uv run python -m benchmarks.immutable``. Input is a synthetic inventory
using real fact models, prepared outside the timings. Freezing projects models
to field mappings, while deepcopy preserves their types; these are distinct
operations. Measurements cover CPU operations only, not persistence or RPC.
"""

import copy
import json
import platform
import statistics
import timeit
from collections.abc import Callable

from benchmarks.facts_cache import workload
from rmote.immutable import freeze


def measure(operation: Callable[[], object], number: int) -> float:
    operation()
    samples = timeit.repeat(operation, repeat=5, number=number)
    return round(statistics.median(samples) * 1_000_000 / number, 3)


def main() -> None:
    _, values = workload(5000, 9)
    snapshot = freeze(values)
    replacement = {"section-0": {"available": False}}
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": 5000,
                "sections": 9,
                "median_us": {
                    "deepcopy_mutable_input": measure(lambda: copy.deepcopy(values), 10),
                    "freeze_mutable_input": measure(lambda: freeze(values), 10),
                    "freeze_snapshot": measure(lambda: freeze(snapshot), 10000),
                    "deepcopy_snapshot": measure(lambda: copy.deepcopy(snapshot), 10000),
                    "read_branch": measure(lambda: snapshot["section-0"], 10000),
                    "replace_one_small_branch": measure(lambda: snapshot.updated(replacement), 1000),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
