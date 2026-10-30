"""Core benchmark runner: timing loop, reporting, viztracer integration."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from .cases import BenchmarkCase


def filter_cases(cases: list[BenchmarkCase], keyword: str | None) -> list[BenchmarkCase]:
    """Filter cases by substring match on name. Returns all cases if keyword is None."""
    if keyword:
        cases = [c for c in cases if keyword in c.name]
    return cases


class BenchmarkResult:
    def __init__(
        self,
        case_name: str,
        description: str,
        entity_count: int,
        pin_count: int,
        times: list[float],
    ):
        self.case_name = case_name
        self.description = description
        self.entity_count = entity_count
        self.pin_count = pin_count
        self.times = times

    @property
    def min(self) -> float:
        return min(self.times)

    @property
    def max(self) -> float:
        return max(self.times)

    @property
    def median(self) -> float:
        return statistics.median(self.times)

    @property
    def entities_per_sec(self) -> float:
        return self.entity_count / self.median if self.median > 0 else float("inf")

    def to_dict(self) -> dict:
        return {
            "case": self.case_name,
            "description": self.description,
            "entity_count": self.entity_count,
            "pin_count": self.pin_count,
            "iterations": len(self.times),
            "times": self.times,
            "min": self.min,
            "median": self.median,
            "max": self.max,
            "entities_per_sec": self.entities_per_sec,
        }


def print_results_table(results: list[BenchmarkResult]) -> None:
    name_width = max(len(r.case_name) for r in results)
    name_width = max(name_width, 4)  # minimum "Case" header width
    header = (
        f"{'Case':<{name_width}} {'Entities':>8} {'Pins':>6} "
        f"{'Min (s)':>9} {'Median':>9} {'Max (s)':>9} {'Ent/s':>10}"
    )
    print()
    print("=" * len(header))
    print("Results")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.case_name:<{name_width}} {r.entity_count:>8} {r.pin_count:>6} "
            f"{r.min:>9.4f} {r.median:>9.4f} {r.max:>9.4f} {r.entities_per_sec:>10.0f}"
        )
    print()


def save_results_json(results: list[BenchmarkResult], path: Path) -> None:
    data = {
        "results": [r.to_dict() for r in results],
    }
    path.write_text(json.dumps(data, indent=2))
    print(f"Results saved to {path}")


def list_cases(cases: list[BenchmarkCase]) -> None:
    """Print available benchmark cases."""
    if not cases:
        print("No cases to list.")
        return
    name_width = max(len(c.name) for c in cases)
    name_width = max(name_width, 4)
    print(f"\n{'Case':<{name_width}}  Description")
    print(f"{'-' * name_width}  {'-' * 60}")
    for c in cases:
        print(f"{c.name:<{name_width}}  {c.description}")
    print()


async def run_case(case: BenchmarkCase, iterations: int) -> BenchmarkResult:
    """Run a single benchmark case for N iterations, return timing results."""
    print(f"\n--- {case.name} ---")
    print(f"    {case.description}")
    print(f"    Setting up...", end="", flush=True)

    times: list[float] = []
    async with case.transfer.global_context(case.entity.generate):
        entity_count = case.transfer.entity_count
        pin_count = case.transfer.pin_count
        print(f" done ({entity_count} entities, {pin_count} pins)")

        for i in range(iterations):
            async with case.transfer.iteration_context():
                start = time.perf_counter()
                await case.transfer.run()
                elapsed = time.perf_counter() - start

            times.append(elapsed)
            print(f"    Iteration {i + 1}/{iterations}: {elapsed:.4f}s")

    result = BenchmarkResult(case.name, case.description, entity_count, pin_count, times)
    print(f"    Median: {result.median:.4f}s ({result.entities_per_sec:.0f} entities/s)")
    return result


async def _trace_single_case(case: BenchmarkCase, output_file: str, tracer_entries: int) -> None:
    """Run a single benchmark case once under viztracer."""
    from viztracer import VizTracer

    print(f"\n--- Tracing {case.name} ---")
    print(f"    {case.description}")
    print(f"    Setting up...", end="", flush=True)

    tracer = VizTracer(output_file=output_file, log_async=True, tracer_entries=tracer_entries)
    tracer.start()

    async with case.transfer.global_context(case.entity.generate):
        entity_count = case.transfer.entity_count
        pin_count = case.transfer.pin_count
        print(f" done ({entity_count} entities, {pin_count} pins)")

        async with case.transfer.iteration_context():
            print(f"    Running under viztracer...")
            tracer.add_instant("run_start", scope="g")
            with tracer.log_event("run"):
                await case.transfer.run()
            tracer.add_instant("run_end", scope="g")

    tracer.stop()
    tracer.save()

    print(f"    Trace saved to {output_file}")
    print(f"    View with: vizviewer {output_file}")


async def trace_cases(
    cases: list[BenchmarkCase],
    output_dir: Path,
    tracer_entries: int = 1_000_000,
) -> None:
    """Run benchmark cases under viztracer, saving traces to output_dir."""
    try:
        import viztracer  # noqa: F401
    except ImportError:
        print(
            "viztracer is not installed. Install it with:\n"
            "  uv sync --group prof\n"
            "or:\n"
            "  uv pip install viztracer"
        )
        return

    if not cases:
        print("No cases to trace.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Tracing {len(cases)} case(s), saving to {output_dir}/")

    for case in cases:
        output_file = str(output_dir / f"{case.name}.json")
        await _trace_single_case(case, output_file, tracer_entries)


async def run_benchmarks(
    cases: list[BenchmarkCase],
    iterations: int = 5,
    json_output: Path | None = None,
) -> None:
    """Run benchmark cases and report results."""
    if not cases:
        print("No cases to run.")
        return

    print(f"Running {len(cases)} benchmark(s), {iterations} iteration(s) each")

    results: list[BenchmarkResult] = []
    for case in cases:
        result = await run_case(case, iterations)
        results.append(result)

    print_results_table(results)

    if json_output:
        save_results_json(results, json_output)
