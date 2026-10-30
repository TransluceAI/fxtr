# sandbox_spawn_benchmark

A spike to measure **where the time goes when spawning a nix-flake container
sandbox** — cold and warm — so we know what to optimize (e.g. whether the
planned env-lifetime builder handle is worth it, versus nix eval/build, podman
VM roundtrips, the per-spawn sidecar tunnel, or guest boot + fxtr2 import).

It reuses the sandbox test suite's harness (`prepare_workdir_repo` +
`create_sandbox_environment`) to stand up a throwaway workdir repo from the
`container_greeter` fixture, then spawns that service `--repeats` times through
a real spawner, collecting `fxtr_nix_runner.perf` spans to print a per-phase
breakdown per spawn plus a cold-vs-warm summary.

## ⚠️ WARNING: `--clear` wipes your caches

`--clear` deletes the shared `fxtr-nix` + `fxtr-nix-cache` podman volumes (the
in-VM `/nix` store and nix eval cache) **and** every delivered fxtr image. The
next spawn cold-starts: it re-fetches the whole fxtr2 closure and rebuilds the
container image, which takes **minutes**. Only use `--clear` when you actually
want a cold-start measurement. It affects every fxtr project sharing that
podman machine, not just this benchmark.

## How to measure

The benchmark itself never clears anything unless you pass `--clear`. The
cold/warm strategy is manual:

- **Warm cache (the common case).** Run *without* `--clear`, twice. The first
  run warms the store/image cache; the second is your clean warm number.
  ```sh
  cd spikes/sandbox_spawn_benchmark
  uv sync
  uv run python -m sandbox_spawn_benchmark --repeats 3   # warms
  uv run python -m sandbox_spawn_benchmark --repeats 3   # clean warm measurement
  ```
  Re-run as many times as you like — nothing is cleared between runs. Within a
  single run, iteration 0 vs iterations 1..N is the *in-process* warm delta
  (note the builder container is still (re)started every spawn on the podman
  backend — that recurring cost is exactly what the summary surfaces).

- **Cold start (⚠ wipes cache).** Run once with `--clear`:
  ```sh
  uv run python -m sandbox_spawn_benchmark --clear --repeats 1
  ```

## Options

- `--repeats N` — spawns per run (default 3).
- `--clear` — ⚠ wipe caches first (see warning).
- `--attr ATTR` — flake service attr to spawn (default `fxtr.services.greeter`).
- `--template DIR` — override the workdir template (default: the in-repo
  `container_greeter` fixture).
- `--workdir DIR` — where to materialize the throwaway repo (default: a temp dir).
- `--json PATH` — also dump the structured `PerfEvent` tree per iteration.
- `--kind {container,wasm}` — `container` only for now (`wasm` is a stub; see
  `PLAN.md`).

## Backend

Uses the configured nix backend (`resolve_backend()` — podman on this machine).
Nothing to set up beyond a running `podman machine`. The template pins the fxtr
library to the current working-copy commit via `fxtr_workdir_harness` (see its
docstring for `FXTR_TEST_LIB_REV` etc.).

## Sample findings (podman/applehv, macOS)

A warm spawn's time was dominated by **container teardown**: `builder.stop`
(`podman rm -f` of the builder) took ~10s because the builder runs `sleep
infinity` as PID 1, which ignores SIGTERM, so `rm -f` waited the full 10s stop
timeout before SIGKILL. Fixed by creating the builder/deliverer/sidecar with
`--stop-timeout=0` (kill immediately — they hold no unflushed state). That took
warm spawn from **~14.3s → ~4.0s**.

Remaining warm breakdown (~4s): `builder.start` ~1.7s (incurred every spawn —
the builder container is recreated per spawn; the planned env-lifetime builder
handle would remove this), `container.sidecar_tunnel` ~0.6s,
`container.run_and_accept` ~0.5s (guest boot + in-container fxtr2 import),
`nix.build.image` ~0.5s (cached), `nix.eval.kind` ~0.4s. Cold start adds the
real image build (~30s) + skopeo copy (~10s). See `PLAN.md` for the full table.

## Ambient perf logging (no benchmark needed)

The instrumentation is in the production code paths, so any spawn anywhere can
print its breakdown:

```sh
FXTR_PERF_LOG=1 <your command that spawns a container>
```

Design notes + the full span list live in `PLAN.md`.
