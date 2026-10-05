"""Compare cold compilation and repeated rendering on fixed template workloads.

Run from the repository root::

    uv run python -m benchmarks.template
    uv run --with jinja2 python -m benchmarks.template
    uv run python -m benchmarks.template --profile loop_if_filters

The Markdown table contains medians of seven timeit batches. Compilation clears
rmote's cache for every call; rendering reuses a compiled template. The frozen
column renders a snapshot of the same context, which the engine accepts without
checking it again. Jinja2 is an
optional reference, with matching output checked before timing. Profile output
is sorted by internal time. No benchmark dependency is required by rmote.
"""

import argparse
import cProfile
import importlib
import platform
import pstats
import statistics
import timeit
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from rmote.immutable import freeze
from rmote.templates import Template


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    context: dict[str, Any]


def cases() -> tuple[Case, ...]:
    hosts = [
        {
            "name": f"host-{i}",
            "address": f"10.0.{i // 256}.{i % 256}",
            "port": 8000 + i % 20,
            "enabled": i % 3 != 0,
            "meta": {"region": {"zone": f"zone-{i % 4}"}},
        }
        for i in range(1000)
    ]
    return (
        Case("scalar", "service={{ name }} port={{ port }}\n", {"name": "web", "port": 8080}),
        Case(
            "loop_1000",
            "{% for host in hosts %}{{ host.name }} {{ host.address }}:{{ host.port }}\n{% endfor %}",
            {"hosts": hosts},
        ),
        Case(
            "loop_if_filters",
            "{% for host in hosts %}{% if host.enabled %}"
            "{{ host.name|upper }}={{ host.address }}:{{ host.port|string }}\n"
            "{% else %}# {{ host.name|lower }} disabled\n{% endif %}{% endfor %}",
            {"hosts": hosts},
        ),
        Case(
            "filters_chain", "{{ names|unique|sort|join(',') }}", {"names": [f"host-{i % 100}" for i in range(1000)]}
        ),
        Case(
            "deep_lookup",
            "{% for host in hosts %}{{ host.meta.region.zone }}={{ host['meta']['region']['zone'] }}\n{% endfor %}",
            {"hosts": hosts},
        ),
        Case("big_context", "{{ hosts|length }}", {"hosts": hosts}),
        # A loop of a size a configuration file really has. The reads of the
        # fields decide the time here, not the size of the context.
        Case(
            "loop_if_200",
            "{% for host in hosts %}{% if host.enabled %}{{ host.name }}={{ host.port }}\n"
            "{% else %}# {{ host.name }} disabled\n{% endif %}{% endfor %}",
            {"hosts": hosts[:200]},
        ),
        # Many names, one read of a few of them: the price of building the
        # context, not of the template.
        Case(
            "wide_context",
            "{{ name_0 }} {{ name_250 }} {{ name_499 }}\n",
            {f"name_{index}": f"value-{index}" for index in range(500)},
        ),
    )


def median_seconds(function: Callable[[], Any], number: int, repeat: int) -> float:
    function()  # Warm imports and interpreter specialization outside the samples.
    return statistics.median(timeit.repeat(function, number=number, repeat=repeat)) / number


def cold_compile(source: str) -> Template:
    Template.compile.cache_clear()
    return Template(source)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main() -> None:
    workloads = cases()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--number", type=positive_int, default=50, help="renders per sample (default: 50)")
    parser.add_argument("--compile-number", type=positive_int, default=100, help="compilations per sample")
    parser.add_argument("--repeat", type=positive_int, default=7, help="number of samples (default: 7)")
    parser.add_argument("--profile", choices=[case.name for case in workloads], help="profile this render workload")
    args = parser.parse_args()

    if args.profile:
        case = next(case for case in workloads if case.name == args.profile)
        template = Template(case.source)
        template.render(**case.context)
        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(args.number):
            template.render(**case.context)
        profiler.disable()
        pstats.Stats(profiler).strip_dirs().sort_stats("tottime").print_stats(25)
        return

    environment = None
    try:
        jinja = importlib.import_module("jinja2")
    except ModuleNotFoundError as exc:
        if exc.name != "jinja2":
            raise
        reference = "Jinja2 unavailable (optional: uv run --with jinja2 python -m benchmarks.template)"
    else:
        environment = jinja.Environment(keep_trailing_newline=True)
        reference = f"Jinja2 {jinja.__version__}"

    print(f"Python {platform.python_version()} / {platform.system()} {platform.machine()}; {reference}")
    print(
        f"1000 hosts × 5 fields; median of {args.repeat} batches; render n={args.number}, compile n={args.compile_number}"
    )
    print(
        "| case | rmote compile µs | rmote render ms | frozen render ms |"
        " Jinja compile µs | Jinja render ms | render ratio | frozen ratio |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for case in workloads:
        template = Template(case.source)
        compiled = environment.from_string(case.source) if environment is not None else None
        if compiled is not None and template.render(**case.context) != compiled.render(**case.context):
            raise ValueError(f"Output differs for {case.name}")
        compile_time = median_seconds(partial(cold_compile, case.source), args.compile_number, args.repeat)
        render_time = median_seconds(partial(template.render, **case.context), args.number, args.repeat)
        # A frozen snapshot is checked once, so a repeated render pays nothing
        # for the size of the context.
        snapshot = freeze(case.context)
        if template.render(snapshot) != template.render(**case.context):
            raise ValueError(f"Frozen output differs for {case.name}")
        frozen_time = median_seconds(partial(template.render, snapshot), args.number, args.repeat)
        reference_times = "— | — | — | —"
        if compiled is not None and environment is not None:
            reference_compile = median_seconds(
                partial(environment.from_string, case.source), args.compile_number, args.repeat
            )
            reference_render = median_seconds(partial(compiled.render, **case.context), args.number, args.repeat)
            reference_times = (
                f"{reference_compile * 1e6:.1f} | {reference_render * 1e3:.3f} | "
                f"{render_time / reference_render:.2f}× | {frozen_time / reference_render:.2f}×"
            )
        print(
            f"| {case.name} | {compile_time * 1e6:.1f} | {render_time * 1e3:.3f} | "
            f"{frozen_time * 1e3:.3f} | {reference_times} |",
            flush=True,
        )


if __name__ == "__main__":
    main()
