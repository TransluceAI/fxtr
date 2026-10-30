"""CLI entry point for store transfer benchmarks.

Usage (from v2 directory):
    uv run python -m examples.benchmarks.store_benchmarks COMMAND [OPTIONS]

Commands:
    run     Run benchmarks
    trace   Run cases under viztracer, saving traces to a directory
    list    List available benchmark cases

Options (shared):
    -k PATTERN          Only include cases whose name contains PATTERN

Options (run):
    -n NUM              Number of iterations (default: 5)
    --json FILE         Save results to JSON file

Options (trace):
    -o DIR              Output directory for traces (default: ./traces)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio

from .cases import ALL_CASES
from .runner import filter_cases, list_cases, run_benchmarks, trace_cases


def _build_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-k", dest="keyword", default=None, help="Filter cases by keyword")

    parser = argparse.ArgumentParser(description="Store transfer benchmarks for fxtr2")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", parents=[parent], help="Run benchmarks")
    run_parser.add_argument(
        "-n", dest="iterations", type=int, default=5, help="Number of iterations"
    )
    run_parser.add_argument("--json", dest="json_output", default=None, help="Save results to JSON")

    trace_parser = subparsers.add_parser(
        "trace", parents=[parent], help="Trace cases with viztracer"
    )
    trace_parser.add_argument(
        "-o", dest="output_dir", default="traces", help="Output directory (default: ./traces)"
    )
    trace_parser.add_argument(
        "--tracer-entries",
        dest="tracer_entries",
        type=int,
        default=1_000_000,
        help="viztracer circular buffer size (default: 1000000)",
    )

    subparsers.add_parser("list", parents=[parent], help="List available cases")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    cases = filter_cases(list(ALL_CASES), args.keyword)

    if args.command == "list":
        list_cases(cases)
        return

    if not cases:
        print(f"No cases matching '{args.keyword}'")
        sys.exit(1)

    if args.command == "trace":
        output_dir = getattr(args, "output_dir", "traces")
        tracer_entries = getattr(args, "tracer_entries", 1_000_000)

        async def _trace() -> None:
            await trace_cases(cases, Path(output_dir), tracer_entries=tracer_entries)

        anyio.run(_trace)

    else:
        iterations = getattr(args, "iterations", 5)
        json_output = getattr(args, "json_output", None)

        async def _run() -> None:
            await run_benchmarks(
                cases=cases,
                iterations=iterations,
                json_output=Path(json_output) if json_output else None,
            )

        anyio.run(_run)


if __name__ == "__main__":
    main()
