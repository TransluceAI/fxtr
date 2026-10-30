"""Benchmark + per-phase timing for nix-flake container sandbox spawns.

Stands up a throwaway workdir repo from the ``container_greeter`` fixture (the
same harness the sandbox test suite uses), then spawns that container service
``--repeats`` times through a real ``create_sandbox_environment``, collecting
``fxtr_nix_runner.perf`` spans so each spawn's per-phase breakdown is visible.

⚠️  ``--clear`` WIPES the shared ``fxtr-nix`` / ``fxtr-nix-cache`` podman
    volumes and every delivered fxtr image — use it only to measure a *cold*
    start (the next spawn re-fetches the fxtr2 closure and rebuilds the image,
    which takes minutes). For warm numbers, run WITHOUT ``--clear`` (ideally
    twice — the first run warms everything, the second is the clean warm
    measurement). See the package README.

Run from the spike dir:

    uv run python -m sandbox_spawn_benchmark --repeats 3            # warm
    uv run python -m sandbox_spawn_benchmark --clear --repeats 1    # cold (⚠ wipes cache)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
from pathlib import Path

import anyio
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_nix_runner import resolve_backend
from fxtr_nix_runner.perf import PerfEvent, collect_perf
from fxtr_sandbox_types import NixContainerServiceDefn, SandboxConnectProto
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

from ._types import GreetingConfig, GreeterProto

# Headline phases surfaced in the cold-vs-warm summary (others still print in
# the per-iteration tree). Order = display order.
_HEADLINE_PHASES = (
    "spawn.build_and_deliver",
    "builder.start",
    "builder.daemon",
    "nix.eval.kind",
    "nix.build.image",
    "deliver.image",
    "builder.stop",
    "container.runtime_detect",
    "container.sidecar_tunnel",
    "sidecar.ensure_image",
    "sidecar.ssh_ready",
    "container.run_and_accept",
    "container.teardown",
)


@dataclasses.dataclass
class IterResult:
    t_spawn: float
    t_connect: float
    totals: dict[str, float]
    events: list[PerfEvent]


def _repo_root() -> Path:
    """Nearest ancestor containing a ``flake.nix`` (the fxtr repo root)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "flake.nix").is_file():
            return parent
    raise RuntimeError("could not locate the fxtr repo root (no flake.nix above this file)")


def _default_template() -> Path:
    return (
        _repo_root()
        / "projects/fxtr_sandboxing/fxtr_sandboxing/tests/_workdir_templates/container_greeter"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_fields(fields: dict[str, object]) -> str:
    return "  (" + " ".join(f"{k}={v}" for k, v in fields.items()) + ")" if fields else ""


def _print_tree(events: list[PerfEvent]) -> None:
    for e in sorted(events, key=lambda e: e.start):
        print(f"    {'  ' * e.depth}{e.name}  {e.elapsed:.3f}s{_fmt_fields(e.fields)}")


def _phase_totals(events: list[PerfEvent]) -> dict[str, float]:
    """Sum elapsed per span name (a phase can, in principle, recur)."""
    totals: dict[str, float] = {}
    for e in events:
        totals[e.name] = totals.get(e.name, 0.0) + e.elapsed
    return totals


def _print_summary(per_iter: list[IterResult]) -> None:
    print("\n=== summary: spawn wall-clock + headline phases (seconds) ===")
    n = len(per_iter)
    cold = per_iter[0]
    warm = per_iter[1:]

    def col(label: str, cold_v: float, warm_vals: list[float]) -> None:
        cold_s = f"{cold_v:7.2f}"
        if warm_vals:
            warm_s = f"{statistics.mean(warm_vals):7.2f}  (n={len(warm_vals)})"
        else:
            warm_s = "     --"
        print(f"  {label:28s}  cold={cold_s}   warm={warm_s}")

    col("spawn() latency", cold.t_spawn, [w.t_spawn for w in warm])
    col("connect()+greet latency", cold.t_connect, [w.t_connect for w in warm])
    for phase in _HEADLINE_PHASES:
        if phase not in cold.totals and not any(phase in w.totals for w in warm):
            continue
        warm_vals = [w.totals[phase] for w in warm if phase in w.totals]
        col(phase, cold.totals.get(phase, 0.0), warm_vals)
    if n == 1:
        print("\n  (only one iteration — pass --repeats N>1 to get an in-process warm column.)")


# ---------------------------------------------------------------------------
# Cache clearing (cold-start prep)
# ---------------------------------------------------------------------------


async def _clear_caches() -> None:
    from fxtr_nix_runner._clear import clear as clear_nix
    from fxtr_sandboxing._container_common import get_container_runtime
    from fxtr_sandboxing._podman import clear_spawn_debris
    from fxtr_sandboxing.nix_container_runner import clear_image_cache

    print("=" * 72)
    print("⚠️  --clear: WIPING the shared nix store/cache volumes + all fxtr images.")
    print("    The next spawn will cold-start (re-fetch fxtr2 + rebuild the image).")
    print("=" * 72)
    removed = await clear_nix()
    for line in removed:
        print(f"  nix: {line}")
    try:
        runtime = get_container_runtime()
        for line in await clear_image_cache(runtime):
            print(f"  image: {line}")
        for line in await clear_spawn_debris(runtime.cmd):
            print(f"  debris: {line}")
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, keep going
        print(f"  (image/debris clear skipped: {exc})")
    print("  cache clear done.\n")


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


async def _run_benchmark(args: argparse.Namespace) -> None:
    if args.kind != "container":
        raise SystemExit(
            f"--kind {args.kind!r} is not implemented in the benchmark yet "
            "(container only for now; see PLAN.md 'Part 4: wasm')."
        )

    backend = resolve_backend()  # configured backend (podman on this machine)
    print(f"backend: {type(backend).__name__}")

    if args.clear:
        await _clear_caches()

    template = Path(args.template) if args.template else _default_template()
    if not template.is_dir():
        raise SystemExit(f"template not found: {template}")

    per_iter: list[IterResult] = []
    async with FxtrScope(name="benchmark"):
        # One-time setup: build the workdir repo (this locks the flake through
        # the backend, so it emits its own spans — timed as "setup").
        with collect_perf() as setup_events:
            t0 = time.perf_counter()
            prepared = await prepare_workdir_repo(template, Path(args.workdir))
            t_prepare = time.perf_counter() - t0
        print(f"\n=== setup: prepare_workdir_repo  {t_prepare:.3f}s ===")
        _print_tree(setup_events)

        defn_ref = NixContainerServiceDefn(
            git_commit=prepared.head_commit,
            flake_attr=args.attr,
        ).store()

        async with (
            create_sandbox_environment(
                cache_dir=Path(args.workdir).parent / "cache",
                git_repo_path=prepared.path,
            ) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()

            for i in range(args.repeats):
                print(f"\n=== spawn iteration {i} ===")
                with collect_perf() as events:
                    async with FxtrScope(name=f"iter{i}"):
                        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

                        t0 = time.perf_counter()
                        sandbox_ref = await spawner.spawn(defn_ref)
                        t_spawn = time.perf_counter() - t0

                        sandbox_ref = sandbox_ref.replace_service_type(
                            SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                        )
                        sandbox = await sandbox_ref.resolve()

                        t1 = time.perf_counter()
                        user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(
                            GreeterProto
                        )
                        user = await user_ref.resolve()
                        greeting = await user.greet("world")
                        t_connect = time.perf_counter() - t1
                        assert greeting == "hello, world!", greeting
                    # iter scope exit tears the sandbox down (captured below).
                _print_tree(events)
                print(f"    -> spawn()={t_spawn:.3f}s  connect()+greet={t_connect:.3f}s")
                per_iter.append(
                    IterResult(
                        t_spawn=t_spawn,
                        t_connect=t_connect,
                        totals=_phase_totals(events),
                        events=events,
                    )
                )

    _print_summary(per_iter)

    if args.json:
        payload = [
            {
                "t_spawn": it.t_spawn,
                "t_connect": it.t_connect,
                "events": [
                    {"name": e.name, "elapsed": e.elapsed, "depth": e.depth, "fields": e.fields}
                    for e in it.events
                ],
            }
            for it in per_iter
        ]
        Path(args.json).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nwrote structured timings to {args.json}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m sandbox_spawn_benchmark",
        description="Benchmark + per-phase timing of nix-flake container sandbox spawns.",
    )
    parser.add_argument("--repeats", type=int, default=3, help="spawns per run (default 3)")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="⚠ WIPE the shared nix volumes + all fxtr images first (cold start)",
    )
    parser.add_argument("--kind", choices=("container", "wasm"), default="container")
    parser.add_argument(
        "--attr", default="fxtr.services.greeter", help="flake service attr to spawn"
    )
    parser.add_argument("--template", default=None, help="override the workdir template dir")
    parser.add_argument(
        "--workdir",
        default=None,
        help="where to materialize the throwaway workdir repo (default: a temp dir)",
    )
    parser.add_argument("--json", default=None, help="also dump structured timings to this path")
    args = parser.parse_args(argv)

    if args.workdir is None:
        import tempfile

        args.workdir = str(Path(tempfile.mkdtemp(prefix="sandbox_spawn_bench_")) / "wd")

    try:
        anyio.run(_run_benchmark, args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
