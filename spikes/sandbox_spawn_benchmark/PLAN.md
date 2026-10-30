# Sandbox spawn benchmark + spawn-path perf instrumentation

## Goal

Two deliverables that answer one question: **where does the tens-of-seconds go
when spawning a `NixContainerServiceDefn` from a flake, cold *and* warm?**

1. **Production perf instrumentation** — a lightweight, opt-in timing helper
   used to wrap the phases of the spawn path across `fxtr_nix_runner` and
   `fxtr_sandboxing`, so *any* spawn (not just the benchmark) can print a
   per-phase breakdown instead of being a silent wait. Off by default; enabled
   by an env var (stderr log) or programmatically (structured collector).

2. **A benchmark spike** (`spikes/sandbox_spawn_benchmark/`) that uses the same
   harness machinery as the sandbox test suite (`prepare_workdir_repo` +
   `create_sandbox_environment`) to stand up a flake and spawn a small container
   service repeatedly, printing the per-phase breakdown so we can compare cold
   vs warm and see which phases dominate.

The output should let us judge how much the planned **env-lifetime builder
handle** ("declare a builder will be reused; keep it alive on first request")
would actually save, versus other costs (nix eval/build, VM roundtrips, the
per-spawn sidecar tunnel, guest process boot + fxtr2 import).

## Background: the container spawn path (phases we want to measure)

One `spawn(NixContainerServiceDefn)` on the podman backend (macOS applehv, a
*remote* machine VM) does, in order:

- **`_spawn_runner_nix_container`** (on `env_tg`):
  - `backend.builder_for(repo)` → on podman this **borrows a per-repo builder
    container** via the refcounted registry. The `async with builder_for(...)`
    block wraps only eval+build+deliver and exits *before* the container runs,
    so unless spawns overlap **the builder is created and torn down every
    spawn**: `podman run -d` + bootstrap (`nix.conf`, `nixbld` users) +
    `_start_daemon` (launch `nix-daemon`, poll `test -S socket` ≤40×@0.25s) +
    `podman rm -f`.
  - `_check_service_kind` → `nix eval` (via `podman exec`).
  - `nix.build_image` → `nix build …#image` (via `podman exec`).
  - `deliver_image` → `_read_platform` (`podman exec cat`), `image_in_runtime`
    (`podman image inspect`); on a cache miss also `_build_skopeo` (`nix
    build`), `_ensure_deliverer` (`podman run -d` + `podman info` for the VM
    socket), skopeo copy.
  - builder block exits → teardown (deliverer + builder `rm -f`).
- **`_run_nix_container_internal`**:
  - `podman_is_remote` → `podman info` (true on macOS machine VM).
  - `open_sidecar_tunnel` (**per spawn**): `ensure_sidecar_image` (`image
    inspect`), `ssh-keygen`, `volume create`, `pod create`, `run -d` sidecar,
    `exec` host-key fetch, `ssh` handshake + READY poll.
  - `podman run --rm --platform … <image_ref>` (the real sandbox), then
    `accept_or_exit` — **this span also captures guest boot + the in-container
    `fxtr_service_runner` Python/fxtr2 import**, a plausibly large cost.
  - teardown: container ladder, then tunnel (`pod rm -f`, `volume rm`).

Every `podman` call above is a separate CLI invocation that roundtrips to the
VM. The instrumentation attributes time at two granularities: **per-phase**
(coarse spans below) and **per-subprocess** (one span inside each package's
`run_process` wrapper, labeled by argv), so a phase expands into the individual
`podman`/`nix` calls it made.

## Part 1: shared perf-span helper

New module **`projects/fxtr_nix_runner/src/fxtr_nix_runner/_perf.py`** (transport
tier — stdlib only: `time`, `logging`, `contextvars`, `contextlib`,
`dataclasses`; **no fxtr2**, so both packages can use it). Cross-package import
of a private module is already the established pattern here (`fxtr_sandboxing`
imports `fxtr_nix_runner._delivery`).

Interface (shapes, not final code):

```python
@dataclass
class PerfEvent:
    name: str            # e.g. "builder.start", "podman info"
    elapsed: float       # seconds
    depth: int           # nesting depth at emit time
    fields: dict[str, str]

@asynccontextmanager
async def perf_span(name, **fields) -> AsyncIterator[None]:
    # push depth (contextvar), time the body, on exit: emit PerfEvent to the
    # active sink (if any) + log at DEBUG to logger "fxtr.perf".

@contextmanager
def perf_span_sync(name, **fields): ...      # same, for sync call sites

@contextmanager
def collect_perf() -> Iterator[list[PerfEvent]]:
    # install a fresh list as the active sink (contextvar) for this task tree;
    # spans append to it; yields the list. The benchmark reads it directly —
    # no log parsing.

def enable_perf_stderr_logging(level=INFO) -> None:
    # attach a compact stderr handler to "fxtr.perf": indented by depth,
    # "<name>  <elapsed>s  (k=v …)". Called from module import when
    # FXTR_PERF_LOG is truthy, or explicitly by callers.
```

Design notes:
- **Nesting** via a `ContextVar[int]` depth counter (works across `await`;
  each anyio task tree inherits the parent's context).
- **Sink** via a `ContextVar[list | None]`. `None` (default) = collection off,
  ~zero overhead. `collect_perf()` sets it for its block only.
- Timing uses `time.perf_counter()`.
- The logger stays silent unless `FXTR_PERF_LOG` is set or a handler is
  attached, so this is inert in production by default.

## Part 2: instrument the production paths

Wrap the phase boundaries with `perf_span`. Two layers:

**(a) Per-subprocess spans (cheap, high value).** Add one span inside each
package's process-runner wrapper so *every* external call is timed and labeled:
- `fxtr_nix_runner/_podman.py::_run` and `_delivery.py::_run`
- `fxtr_sandboxing/_podman.py::_run_checked` and
  `nix_container_runner.py::_run_checked`
- `HostHandle.run_nix` / `PodmanHandle.run_nix` (label `nix <subcommand>`).

Label = first 2–3 argv tokens compacted (e.g. `podman info`, `podman run`,
`podman exec nix build`). This directly quantifies the "many serial VM
roundtrips" hypothesis.

**(b) Coarse phase spans** at the semantic boundaries:
- `fxtr_nix_runner/_podman.py`: `builder.start` (around `_start_builder`),
  with child spans `builder.volumes`, `builder.run`, `builder.bootstrap`,
  `builder.daemon` (record poll iteration count + waited time as fields);
  `builder.stop` (around `_stop_builder`).
- `fxtr_nix_runner/_podman.py` + `_host.py` `deliver_image`: `deliver.total`
  with children `deliver.read_platform`, `deliver.image_in_runtime`,
  `deliver.build_skopeo`, `deliver.ensure_deliverer`, `deliver.skopeo_copy`
  (skopeo children skipped on a cache hit — the absence is itself the signal).
- `fxtr_sandboxing/nix.py`: `nix.eval.kind`, `nix.eval.entryPoint`,
  `nix.build.image`, `nix.build.sitePackages`, `nix.materialize` (wrap the
  existing `_run_nix` call sites / the `materialize` call).
- `fxtr_sandboxing/nix_container_runner.py::_run_nix_container_internal`:
  `container.runtime_detect`, `container.sidecar_tunnel` (whole
  `open_sidecar_tunnel`), `container.run_and_accept` (from `open_process` to a
  successful `accept_or_exit` — **captures guest boot + fxtr2 import**),
  `container.teardown`.
- `fxtr_sandboxing/_podman.py::open_sidecar_tunnel`: `sidecar.ensure_image`,
  `sidecar.keygen`, `sidecar.volume`, `sidecar.pod`, `sidecar.run`,
  `sidecar.hostkey`, `sidecar.ssh_ready`.
- `fxtr_sandboxing/spawner.py`: `spawn.nix_container` (whole runner body up to
  `started`) and — cheap, per answer #2 — `spawn.nix_wasm` around the wasm
  runner (it reuses `builder.*` + `nix.*` spans, just no delivery/container).

No behavior changes — only spans added. Every span nests under whatever the
caller opened, so a single top-level `spawn.nix_container` expands into the
full tree.

## Part 3: the benchmark spike

Layout under **`spikes/sandbox_spawn_benchmark/`**:

```
spikes/sandbox_spawn_benchmark/
  pyproject.toml            # uv project; deps: fxtr2, fxtr-sandboxing,
                            #   fxtr-sandbox-types, fxtr-nix-runner,
                            #   fxtr-workdir-harness, anyio
  README.md                 # usage + the cache-clearing WARNING (see below)
  src/sandbox_spawn_benchmark/
    __init__.py
    _types.py               # GreetingConfig + GreeterProto mirror (copied from
                            #   test_nix_container_spawner: same RPC_TASK_NS UUID
                            #   + define_type strings → wire-compatible)
    __main__.py             # the CLI / driver
```

**Test package**: reuse the existing `container_greeter` template at
`projects/fxtr_sandboxing/fxtr_sandboxing/tests/_workdir_templates/container_greeter`
(per answer #3). Locate it by walking up for the repo root (`flake.nix`) like
the harness does; allow `--template PATH` to override. (We reference the
existing template rather than copying it, to avoid drift; documented in README.)

**CLI** (`python -m sandbox_spawn_benchmark`):
- `--repeats N` (default 3): spawns per run. Iteration 0 vs the rest is the
  in-process warm delta (builder churn etc. still recur every spawn — that's
  the point).
- `--clear`: **before running**, blow away caches to measure a true cold start
  (see warning). Off by default.
- `--kind {container,wasm}` (default container).
- `--attr` (default `fxtr.services.greeter`), `--template PATH`.
- `--json PATH`: also dump the structured `PerfEvent` tree per iteration.

**Flow** (async, `anyio.run`):
1. `resolve_backend()` (uses the configured backend — podman, per answer #5).
2. If `--clear`: print a loud banner, then run
   `fxtr_nix clear` + `python -m fxtr_sandboxing.nix_container_runner
   clear-images` + `… clear-spawn-debris`. (Invoke via subprocess / the modules'
   functions.) This removes the shared `fxtr-nix`/`fxtr-nix-cache` volumes and
   the delivered-image store → the next run re-fetches/rebuilds everything.
3. `perf`-time `prepare_workdir_repo(container_greeter, tmp)` (it locks the
   flake through the backend — a real cost worth showing) → `PreparedWorkdir`.
4. `async with create_sandbox_environment(cache_dir=…, git_repo_path=prepared.path)`.
5. For `i in range(repeats)`: `with collect_perf() as events:` →
   `spawn(defn)` → `connect(cfg)` → one `greet(...)` call → let the refs drop /
   teardown. Stash `events` + wall-clock per iteration.
6. Render: for each iteration, an indented tree of `name  elapsed  (fields)`;
   then a summary comparing iteration 0 (cold-ish) vs the mean of 1..N (warm)
   per top-level phase.

**Cold/warm strategy (documented in README, per answer #4 — manual, not
automatic):**
- **Cold start**: run once with `--clear`. ⚠️ This *deletes the shared nix
  store volume and image cache* — the first spawn after will re-fetch the fxtr2
  closure and rebuild the image (minutes). Only use when you want cold numbers.
- **Warm cache**: run *without* `--clear`, ideally twice — the first run warms
  everything, the second is a clean warm measurement. Re-run as many times as
  you like; nothing is cleared between runs.

README carries a prominent **WARNING: `--clear` wipes your shared `fxtr-nix`
podman volume and all delivered fxtr images.**

## Part 4: wasm (optional, cheap — per answer #2)

`--kind wasm` builds a `NixWasmServiceDefn` against the wasm greeter template
(`projects/fxtr_sandboxing/.../tests/_workdir_templates/greeter`) and spawns via
the eryx arm. It reuses the `builder.*` + `nix.*` spans; the only wasm-specific
span is around `run_in_factory` (eryx startup). Include if it drops in cleanly;
otherwise leave a TODO and ship container-only first.

## Testing / how to run

```sh
# warm (run twice; second is the clean warm number)
cd spikes/sandbox_spawn_benchmark
uv run python -m sandbox_spawn_benchmark --repeats 3
uv run python -m sandbox_spawn_benchmark --repeats 3

# cold (⚠ wipes the shared nix store + image cache)
uv run python -m sandbox_spawn_benchmark --clear --repeats 1

# any spawn, anywhere, now prints phase timings:
FXTR_PERF_LOG=1 uv run pytest -n0 -k test_spawn_nix_container_service_greet
```

Sanity: `uv run pyright` in both touched packages; the existing container
E2E test still passes with instrumentation added (spans are inert when no sink
/ handler is active).

## Implementation steps

- [x] Add `perf.py` to `fxtr_nix_runner` (public module — chose a public name
      over the transport-tier `_`-private convention since consumers import it;
      span + sync span + `record` + collector + `proc_label` + stderr-logging
      enable; `FXTR_PERF_LOG` auto-enable). Unit-tested (`tests/test_perf.py`):
      collector nesting/start-order, task-independent capture, inert-by-default.
- [x] Update `fxtr_nix_runner/CODE_OVERVIEW.md` (new `perf.py` + module note +
      test).
- [x] Instrument `fxtr_nix_runner`: `_podman._run`/builder+deliver phase spans,
      `_delivery._run`, `_host.run_nix`/`deliver_image`.
- [x] Instrument `fxtr_sandboxing`: `nix.py`, `spawner.py` (both nix arms),
      `nix_container_runner.py` (runtime_detect / sidecar_tunnel /
      run_and_accept / teardown), `_podman.py` (sidecar ensure_image/ssh_ready/
      teardown).
- [x] Update `fxtr_sandboxing/CODE_OVERVIEW.md` (Spawn perf timing section).
- [x] Create the `spikes/sandbox_spawn_benchmark/` uv package (pyproject,
      `_types.py`, `__main__.py`, README with the cache-clear WARNING).
- [x] Manual run: warm ×N; recorded the breakdown below + in the README.
- [x] **Bonus fix found by the data:** `--stop-timeout=0` at container creation
      (builder, deliverer, sidecar) — see Findings.
- [ ] (Optional) wasm arm — deferred (`--kind wasm` is a stub; low interest).

## Findings (measured on the podman/applehv backend, macOS)

Setup: `container_greeter` fixture, `--repeats 3`, warm = mean of iterations
1–2 (iteration 0 rebuilds when the pinned lib rev changed).

**The dominant warm cost was container teardown, not build.** Before any fix, a
warm spawn was **~14.3s**, of which **`builder.stop` alone was ~10.2s** — a
single `podman rm -f` of the builder. Root cause: the builder/deliverer/sidecar
run `sleep infinity` as PID 1, which ignores SIGTERM, so `rm -f` waited the full
10s stop timeout before SIGKILL. Verified directly: `podman rm -f` = 10.2s vs
`podman rm -f -t 0` = 0.19s.

Fix applied: `--stop-timeout=0` at container **creation** (not `-t 0` per
teardown), so every removal path — normal teardown, bootstrap-failure cleanup,
`fxtr_nix clear`, `clear_spawn_debris` — kills immediately. (Rejected `--init`:
the only process that might want graceful shutdown, the builder's nix-daemon, is
`podman exec`'d and so not reachable via PID-1 signal forwarding; and teardown
never fires with work in flight.)

**Result: warm spawn 14.3s → ~4.0s.** New warm breakdown:

| phase | warm (s) |
|---|---|
| `builder.start` (per spawn!) | ~1.66 |
| `container.sidecar_tunnel` | ~0.60 |
| `container.run_and_accept` (guest boot + fxtr2 import) | ~0.47 |
| `nix.build.image` (cached) | ~0.46 |
| `nix.eval.kind` | ~0.39 |
| `builder.stop` | ~0.18 |
| `deliver.image` (cache hit) | ~0.16 |

**Next optimization target: `builder.start` (~1.66s), incurred on *every*
spawn** because `builder_for` refcounts to zero between spawns and recreates the
container (+ re-bootstraps + relaunches nix-daemon). This is exactly what the
planned **env-lifetime builder handle** (declare-and-keep-alive) removes: it
would drop `builder.start` + `builder.stop` (~1.8s) and keep the daemon warm,
taking warm spawn to ~2.2s. After that, the sidecar tunnel + guest-boot (~1.1s
combined) are the next frontier.

Cold-start costs (first build of a new lib rev): `nix.build.image` ~28–35s and
`deliver.skopeo_copy` ~10s dominate — inherent to building/copying a full-fxtr2
linux image; not addressed here.

### Update — warm builder (stage 2p4)

`projects/fxtr_sandboxing/plans/stage2/2p4_persistent_spawner/plan.md` shipped:
the spawner now holds one env-lifetime `keep_builder_warm` pin, so the builder
container is started once per env instead of per spawn. On warm iterations
`builder.start` / `builder.stop` / `builder.daemon` no longer fire at all.

**Warm spawn ~4.0s → ~2.36s.** New warm breakdown:

| phase | warm (s) |
|---|---|
| `spawn.build_and_deliver` | ~1.04 (nix.eval.kind ~0.39 + nix.build.image ~0.45 + deliver ~0.21) |
| `container.sidecar_tunnel` | ~0.65 |
| `container.run_and_accept` | ~0.55 |
| `container.runtime_detect` | ~0.10 |

Full progression: **14.3s → 4.0s (`--stop-timeout=0`) → 2.36s (warm builder).**

### Update — build-pipeline cache + micro-caches (plan3)

`plan3_spawn_latency.md` shipped: an in-env cache keyed by
`(commit, attr, flake_path)` so a **repeat spawn of the same service** skips
eval/build/deliver (`spawn.cache_hit`), plus memoizing `podman_is_remote` and the
sidecar host key. (The "merge kind+build into one eval" idea was spiked and
**killed** — any `nix eval` that forces `.image` is ~2s because the flake eval
cache serves `nix build`, not `nix eval`; see plan3.)

**Warm *repeat* container spawn: ~2.36s → ~1.08s.** Breakdown: `build_and_deliver`
+ `runtime_detect` → ~0 (cached), sidecar tunnel ~0.54s, container run/guest-boot
~0.50s. First spawn of a new service is ~2.15s (only the micro-caches apply).

Full progression (warm repeat): **14.3s → 4.0s → 2.36s → 1.08s.** The remaining
~1.1s (per-spawn sidecar tunnel + container boot + fxtr2 import under Rosetta) is
the structural floor — and much of it is the macOS podman-machine RTT tax, not a
fxtr-design cost.
```
