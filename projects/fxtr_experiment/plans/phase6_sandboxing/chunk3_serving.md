# Chunk 3 — serving through fxtr_sandboxing

Sandboxed bindings actually serve: resolving a sandbox-bound key
spawns (or reuses) a sandbox via `fxtr_sandboxing` and connects, with
the pinned dependency map resolved eagerly host-side. Ephemeral
bindings gain the same optional dependency map + config (the two
classes become fully uniform in what the bound implementation
receives). Design: `notes/sandboxed_bindings.md`; the chunk-start
rationale trail is the chunk-3 round in `discussion_log.md` (this
directory).

**Prerequisite commits** (each self-contained, landed first):

1. [chunk3pre1_auto_kind_spawn.md](chunk3pre1_auto_kind_spawn.md) —
   `fxtr_sandboxing` gains the kind-agnostic `NixServiceDefn` +
   auto-dispatch spawn arm, so this chunk spawns from the stored
   triple with no kind knowledge.
2. [chunk3pre2_escape_classification.md](chunk3pre2_escape_classification.md)
   — the shared escape-classification rule; safety-pass setup faults
   map to `interrupted`. All the new fault-class errors below ride
   that rule at both executor spawn sites.

A followup commit is scoped but deliberately **not** in this chunk:
[chunk3post3_package_split.md](chunk3post3_package_split.md) (the
physical task-side package split; this chunk lands the module split
that is its prerequisite).

## Decisions (chunk-start round)

1. **v0 runtime split.** Task-fn bindings target runtimes where
   `fxtr_experiment` installs directly — **container** now (pyodide
   should ideally also work later; not verified in v0). Wasm task-fn
   guests are out of scope: they are blocked twice (the import graph
   pulls the runner side, and the package hard-depends on polars,
   which has no wasm wheel) — the module split below fixes the first
   blocker; the package split (chunk3post) fixes the second.
   Step-connected *plain* services work on **any runtime** (the guest
   implements its own protocol against `fxtr_sandbox_types` +
   whatever fxtr2 flavor its runtime supports). Documented in
   `notes/sandboxed_bindings.md`.

2. **The task-side module split** (in this chunk; the boundary the
   package split will later cut along): `TaskFnAdapter` / `task_fn` /
   `normalize_outputs` / `_TaskContext` move from `task_service.py`
   to a new **`task_adapter.py`**. After the move the task-side
   import graph — `task_api`, `views`, `view_spec`, `view_runtime`,
   `runner_api`, `model`, `errors`, `task_adapter` — is free of the
   runner side (`ops_engine` → `state_store`/`storage`/sqlite) and
   polars-lazy. A small **import-boundary test** loads the task-side
   set with the runner-side modules blocked (and asserts no eager
   polars/sqlite3), so the boundary can't rot. No re-export shims:
   importers (core, tests, demo drivers) update to the new module.

3. **One binding authority: `BindingHost`** (new module
   `bindings.py`; `BindingRegistry` dissolves into it —
   `task_service.py` keeps only the runner-side service boundary,
   `RunCapabilityImpl`). The host owns:
   - the **ephemeral entries** (connector ref + pinned dependency map
     + canonical config text) and their bind/release lifecycle
     (`ConnectorProto`, `wrap_singleton`, `BindingRelease` move here);
   - the **session** reference (durable-record lookup + the
     unrecorded pin/dep reads — the records themselves stay
     session-owned, unchanged from chunk 2);
   - the **launcher** (below), `sandbox_repo`, and the **sandbox
     runtime state**: the per-binding process cache with in-flight
     dedup, and the launch-state map;
   - the one **`spawn` funnel** (below), `shutdown_sandbox`, and the
     change listener (launch-state transitions and sandbox
     cache changes emit the `bindings` hint like binds/releases do).

   The executor (both spawn sites) and `StepHost` carry the host —
   same `spawn(handle, *, into_scope)` shape they already use, so
   `ops_engine` changes are type/wording only.

4. **The connect-time funnel** (both classes; serving is uniform):

   ```
   spawn(handle, into_scope, in_progress=frozenset()):
     handle.id in in_progress        → binding-cycle fault
     entry = ephemeral entry | session sandbox record
                                     | raise HandleUnboundError
     for each pinned dep (dep_name-sorted):
       re-read the dep cell (unrecorded session read)
       empty / grid or cell missing / pin mismatch → dependency fault
       dep_ref = spawn(dep_key, into_scope, in_progress | {handle.id})
     request = BindingConnectRequest(key=handle, config, handles)
     ephemeral  → connector.connect(request)
     sandboxed  → repo unconfigured → repo fault
                  process = cache.get_or_launch(key)   # in-flight dedup
                  process.connect(request)             # fresh dispatch scope
   ```

   Dependencies resolve eagerly, **before** the outer launch (a
   not-ready dependency faults before any sandbox spawns). The
   in-progress set is an **explicit parameter** threaded through the
   recursive dep spawns (direct calls within one connect — no
   ContextVar, no global; anyio-safe by construction). Pin-check
   reads are runner machinery: unrecorded, no generation observation.
   No spawner or registry capability enters any guest — only the
   named pre-spawned refs.

5. **The launcher seam** (new module `sandbox_launcher.py` — the sole
   module importing `fxtr_sandboxing`):

   ```
   class SandboxLauncherProto(Protocol):
       async def launch(defn: SandboxDefn, *, into_scope) -> LaunchedSandbox
   ```

   where `LaunchedSandbox` exposes `connect(request) →
   ServiceRef[UnknownService]` and the handle the host releases on
   shutdown. The real impl lazily opens **one
   `create_sandbox_environment` per runner on first launch**
   (`cache_dir` + `git_repo_path=sandbox_repo`), then per launch:
   store a transient `NixServiceDefn` from the triple →
   `spawner.spawn` →
   `replace_service_type(SandboxConnectProto[BindingConnectRequest,
   UnknownService])`. The fake for the core suite implements the same
   protocol (`spawn(defn) → connectable`), so every funnel behavior
   is testable sandbox-free. Teardown ordering rides `open_local`'s
   exit stack: cached guest holds released → env closed → runner
   scope drains.

   `cache_dir`: `open_local(..., sandbox_cache_dir: Path | None =
   None)`, defaulting to a machine-local user-cache path
   (`~/.cache/fxtr_experiment/sandboxes`); ephemeral machine state
   like `sandbox_repo`, never persisted or exported.

6. **Shutdown / remove semantics: release-and-forget.** Cached spawn
   refs are held via **independent leases** on a host-owned scope (a
   child of the runner scope), so release is non-blocking.
   `shutdown_sandbox(handle)` (new wire method) and
   `remove_sandbox_binding` both: drop the cache entry + release the
   lease synchronously, launch state → `unlaunched`; the next use
   relaunches fresh. **Already-connected guests stay connected** —
   their chain-bridged holds keep the process alive, and it exits
   when the last run's scope drains (no force-kill anywhere in the
   normal path). A test proves an in-flight call survives
   `shutdown_sandbox`. `shutdown_sandbox` on a binding with no cached
   process is a no-op; on an unknown binding it faults like
   `remove_sandbox_binding` does.

7. **Ephemeral uniformity.** `bind_handle` grows `deps: Mapping[str,
   ViewSel] = {}` and `config: JSONValue | None = None`, with
   bind-time semantics identical to `bind_sandbox` (fully-bound
   handle cells, populated, pinned at bind; canonical config text) —
   the resolve/read/pin half factors into a shared helper used by
   both capability methods (the sandbox class keeps its atomic
   in-transaction session body from chunk 2). Ephemeral pins live on
   the in-memory entry and die with release, consistent with the
   class's lifecycle.

8. **Failure surfacing.** Launch failures (nix eval/build, missing
   commit, spawn) carry the **full** underlying message — nix stderr
   included, unbounded (bounding was considered and rejected: no
   evidence yet that size is a problem; presentation-layer concern if
   it becomes one). Launch states: `unlaunched` / `launching` /
   `running` / `faulted`, runtime-only (the `run_log` precedent —
   never persisted). `faulted` means "the last launch attempt failed
   and nothing is in flight", carries the failure message +
   monotonic-ish attempt info for the panel, and clears on the next
   demand's attempt (→ `launching`) or `shutdown_sandbox` (→
   `unlaunched`). Repeated failing builds re-attempt per demand
   (negative caching stays deferred).

9. **New errors** (`errors.py`), all in the fault-class family so
   both executor spawn sites and step-connect poisoning classify them
   as interruptions via pre-commit 2's rule:
   - `SandboxRepoUnconfiguredError(CellFaultError)` — sandbox-bound
     key demanded with no `sandbox_repo` configured (names the
     `open_local` kwarg as the remediation);
   - `BindingDependencyFaultError(HandleUnboundError)` — connect-time
     dependency not-ready: empty cell, missing grid/cell, or pin
     mismatch (message names the dep, the pinned id, and what was
     found);
   - `BindingCycleError(CellFaultError)` — the in-progress set saw a
     repeat key (message names the cycle path);
   - `SandboxLaunchError(CellFaultError)` — eval/build/spawn failure,
     full stderr in the message.
   (Bind-time validation keeps chunk 2's interactive
   `SandboxDependencyError`; these are the connect-time siblings.)

10. **`BindingInfo` growth** (`user_api.py`): `BindingState` gains
    `sandbox_launching` / `sandbox_running` / `sandbox_faulted`
    (replacing `sandbox_launchable` as the reported state when a
    process exists or an attempt is in flight/failed;
    `sandbox_launchable` stays the idle repo-configured state);
    `SandboxBindingInfo` gains the fault message (`None` unless
    faulted). Ephemeral entries' deps/config surface through the same
    `SandboxBindingInfo`-shaped detail so `list_bindings` reports
    both classes uniformly (field naming to settle at implementation;
    the `external` state keeps meaning "ephemeral connector").

11. **Fixtures — two, plus the fake.**
    - **wasm plain service** (step-connect e2e): a workdir-template
      git repo under `tests/_workdir_templates/` following
      `fxtr_sandboxing`'s greeter convention; attr kind `wasm`;
      entrypoint takes `BindingConnectRequest`, echoes key/config,
      and calls a wired dependency ref (proving `handles` refs work
      in-guest).
    - **container task-fn service** (executor e2e): a guest package
      installing `fxtr_experiment` (+ full fxtr2 + polars in the
      image); entrypoint mounts a `TaskFnAdapter`-wrapped fn and
      returns it as the `WorkflowTaskFnProto` service. Needs a
      committed `uv.lock` (the known spawn-env gotcha). This is the
      proof that grants/capability refs bridge through the sandbox
      RPC boundary.
    - Gating per `fxtr_sandboxing` conventions: the `nix_runtime`
      marker + `skip_if_runtime_unconfigured`; the container test
      additionally follows the container-runtime gating. The core
      suite runs entirely on the fake launcher.

## Implementation steps

- [x] Land pre-commits 1 and 2 (their own plan files).
- [x] **Module split**: `task_adapter.py` (move `TaskFnAdapter`,
      `task_fn`, `normalize_outputs`, `_TaskContext`); update all
      importers (core, tests, `examples/fxtr_experiment_demo`
      drivers); the import-boundary test.
- [x] **`errors.py`**: the four fault-class errors (decision 9).
- [x] **`sandbox_launcher.py`**: `SandboxLauncherProto` /
      `LaunchedSandbox`; the real `fxtr_sandboxing`-backed impl with
      the lazy per-runner environment; `fxtr_experiment` gains the
      `fxtr_sandboxing` dependency.
- [x] **`bindings.py`**: `BindingHost` per decisions 3–6 —
      ephemeral entries with pins/config, the funnel with recursive
      dep resolution + cycle check, the per-binding process cache
      with in-flight dedup and launch states, `shutdown_sandbox`,
      change-hint emission; `ConnectorProto` / `wrap_singleton` /
      `BindingRelease` move in; `BindingRegistry` deleted from
      `task_service.py`.
- [x] **`state_store.py`**: the shared resolve/read/pin helper split
      out for `bind_handle`'s use (the sandbox body keeps its atomic
      transaction); an unrecorded read suited to pin checks if
      `read_cell` doesn't already serve it.
- [x] **Executor / `ops_engine`**: carry `BindingHost` (`StepHost`,
      both executor spawn sites — type + docstring updates; behavior
      rides the funnel).
- [x] **`user_api.py`**: `bind_handle` grows `deps`/`config`;
      `shutdown_sandbox`; `BindingState` + `SandboxBindingInfo`
      growth (decision 10) — full docstrings (uniform request
      contract, release-and-forget semantics, launch states).
- [x] **`user_service.py`**: impls over the host; `open_local` grows
      `sandbox_cache_dir`, constructs launcher + host, owns teardown
      ordering; `Workflow` wrapper passthroughs; `list_bindings`
      serves launch state + fault message.
- [x] Update `CODE_OVERVIEW.md` (new modules, task_service slimming,
      the phase-6 TODO noting chunk 3 done).
- [x] **Fake-launcher unit coverage** (core suite, sandbox-free):
      funnel record→launch→connect for both classes; in-flight dedup
      (concurrent demands share one launch); pin
      empty/missing/mismatch faults; dependency recursion +
      cycle fault; repo-unconfigured fault; launch failure →
      `faulted` state + full message, cleared on next attempt;
      `shutdown_sandbox` release-and-forget + relaunch;
      remove-while-cached; ephemeral deps/config uniformity;
      executor + step-connect paths land `interrupted` on these
      faults (incl. through the safety pass, via pre-commit 2);
      `list_bindings` states; the `bindings` watch sees launch-state
      transitions.
- [x] **E2E tests** (gated): wasm fixture through step-connect
      (spawn, connect, config + key + dep ref in-guest, replay hit on
      re-demand); container task-fn fixture through the executor
      (run + safety pass, results persisted); in-flight call survives
      `shutdown_sandbox`. (Plus the pyodide sweep of the step-connect
      fixture and the bisect/regression variants — see the deviations.)
- [x] **Docs**: `notes/sandboxed_bindings.md` (v0 runtime split,
      release-and-forget, launch states, `sandbox_cache_dir`,
      auto-kind spawn via `NixServiceDefn`, the funnel's cycle
      check); `notes/execution_semantics.md` embodiment section if
      the scope tree wording needs the host; `design_overview.md`
      decision map (the binding bullet's mechanics pointer:
      `bindings.py`); final `CODE_OVERVIEW.md` pass.

## Deviations and findings

- **The seam protocols live in `bindings.py`**, not
  `sandbox_launcher.py`: the consumer defines the seam, so the core
  never imports `fxtr_sandboxing` — which also became an
  **optional `[sandbox]` extra** (+ dev-group member) instead of a hard
  dependency: a container task-fn *guest* installs `fxtr_experiment`
  and must not inherit the host sandbox machinery (eryx, aiohttp,
  podman drivers). `open_local(sandbox_repo=…)` imports the real
  launcher lazily.
- **Launcher shape**: `launch(defn) -> LaunchedSandbox` with
  `connect(request, *, into_scope)` (the plan sketch had `into_scope`
  on `launch`). Lifetimes use the **holder-task pattern**
  (`_serve_spawn_rpc` precedent) rather than raw lease-release:
  `FxtrScope`'s CM co-manages an internal task group, so a dynamic-
  lifetime scope must live inside its own task parked on a teardown
  event — `shutdown()` just sets the event. Release-and-forget then
  falls out: each *connect* takes an independent lease on the
  process's holder scope from the run scope, so the holder's drain
  waits exactly for in-flight runs. Launch holder scopes include the
  env host scope (spawner refs resolve through them), giving the
  guests-before-env teardown order for free.
- **`BindingInfo` shape**: `deps` + `config` moved from
  `SandboxBindingInfo` up to `BindingInfo` (both classes carry them —
  the uniformity argument); `SandboxBindingInfo` is now defn +
  `fault_message`.
- **Session additions**: `get_sandbox_binding` (the funnel's lookup)
  and `pin_dependency_cells` (the shared bind-time pin read; the
  sandbox insert body reuses the same `_pin_dep_cells` sync helper).
- **In-flight dedup shares failures**: demands that joined an
  in-flight launch share its outcome (fault included); only a *later*
  demand replaces a faulted slot and re-attempts. A launch cancelled
  mid-flight clears its slot (waiters fault retryably; next demand
  relaunches).
- **The chunk-2 regression test's expectation changed**: a
  sandbox-bound key on a launcher-less runner now faults
  `SandboxRepoUnconfiguredError` (was `HandleUnboundError`-not-ready) —
  same fault family, better remediation message.
- **RESOLVED — the e2e teardown deadlock** (the investigation record,
  every experiment included: [chunk3_scope_map.md](chunk3_scope_map.md)).
  Root cause: the launcher's lazily-started holder tasks inherited the
  demanding dispatch's ACTIVE_SCOPE via task context, so the sandbox
  environment's long-lived scopes derived from an ephemeral per-call
  scope, which then could never end while the process lived — parking
  that dispatch's exit and (in the e2e) the step scope behind it. Fix:
  capture ACTIVE_SCOPE at launcher construction and reset it at the
  top of each holder task. What the hunt left behind: the regression
  lives on as `tests/test_sandbox_launcher_scopes.py` (the real
  launcher over a fake, hazard-preserving environment, core-suite
  cost — verified to deadlock with the fix reverted); the pyodide e2e
  sweep (born as a runtime discriminator, kept as real coverage of
  the any-runtime claim for step-connected plain services); and the
  ``anyio.fail_after`` guards on the e2e tests (env-overridable
  ``FXTR_E2E_TIMEOUT``) so hangs fail loudly. The raw debug residue
  (the repro ladder, the e2e bisect variants incl. the (d)/(e)
  minimal pair, the task-dump + scope-lease-dump helpers and their
  ``fxtr-sandbox-rpc-adapter`` dev dep, and a minimal in-memory
  example of the failure class) is preserved in a branching,
  deliberately-abandoned `debug residue` commit on top of this
  chunk's commit — reference only, never built upon. Root-cause
  follow-ups (the fxtr_sandboxing scope fix, tooling/guideline
  safeguards) are planned as chunk3post files in this directory.
- **Container-fixture verification loops through the workdir mirror**:
  `prepare_workdir_repo` pins the library at jj `@` via the cache
  mirror, so the `binding_taskfn` image builds against the working
  copy; a source change during iteration means re-preparing the
  fixture (and the image build is the phase's slowest test by far).
