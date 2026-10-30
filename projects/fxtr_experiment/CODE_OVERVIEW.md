# fxtr_experiment — code overview

A durable workflow system on top of `fxtr2` (the entity / service /
scope machinery in `v2/`). A workflow is a global namespace of
**grids** — tables whose cells are indexed by coordinate axes and
typed as data (entities or dataframes), service **handles**, **task**
results (with named entity/dataframe outputs), **checkpoints**, or
**subtrees** of further grids. Task grids carry stored **rules**
wiring task fns to input views; execution is durable and
demand-driven (record-check/replay, checkpoints); the records double
as observation links from which **currency** (fresh / stale /
unsettled) is derived, and user sourcing records + **targets** root
the second derived axis, **grounding** (would a from-scratch replay
re-create this?). The top-level user drives the same view calculus
through one privileged capability, and a workstation **dashboard**
renderer monitors and manipulates it from the browser.

**Where to start reading**: `notes/design_overview.md` is the ROOT
SOURCE OF TRUTH — it carries the reading order, the decision map
(pointing at the detailed writeups and module docstrings), open
questions, and deferred followups. The concept reference for
holders/frames/positional addressing is `view_spec.py`'s module
docstring; the dynamic model is `notes/execution_semantics.md`.

## How this project is developed

Design discussion happens iteratively: rounds land in
`notes/discussion_log.md` (a working document; completed phases are
archived under `notes/archive/`), and settled decisions are
consolidated into `notes/design_overview.md` or the relevant module
docstrings — the log is never the only home of a decision. Interfaces,
protocols, and base classes carry Google-style docstrings (the
contracts); library code follows `v2/notes/scope_discipline.md` and
every wire surface follows `notes/boundary_discipline.md`. Work is
planned per phase under `plans/`.
TODO: phase 4 (the monitoring UI,
`plans/phase4_monitoring_ui/overview.md`) is in progress — the watch
subscriptions (backend + renderer; `notes/event_system.md`) are done,
as are the post-verification refinements
(`plans/phase4_monitoring_ui/chunk6post1.md`), task-level `log()`
(`plans/phase4_monitoring_ui/chunk7_part1_task_logging.md`), and the
renderer consolidation — the repo nix lib slice + the repin/reload
dev loop (`plans/phase4_monitoring_ui/renderer_consolidation.md`);
remaining: chunk6post1's deferred update-ripple item and the optional,
needs-scoping output-channel system
(`plans/phase4_monitoring_ui/chunk7_part2_channels.md`).
TODO: the flagged end-of-phase-5 design revisit — **targeting as
expressions** (per-cell target records replaced by a demand-expression
DSL, possibly dropping the demand-requires-target rule for a warning;
sketch: the phase 5 discussion log) — is still open; phase 5 itself
(dataframes: encoding, frame runtime, cell ops, gather, dashboard
frame rendering) is complete.
TODO: phase 6 (**sandboxed bindings**,
`plans/phase6_sandboxing/overview.md`) is in progress; the canonical
design is `notes/sandboxed_bindings.md` (plus the binding passages of
the design overview / at-rest / execution-semantics notes). Done:
chunk 1 (the uniform `BindingConnectRequest` contract); chunk 2
(**durable records + capability surface** — the
`sandbox_bindings`/`sandbox_binding_deps` state-db tables + session
CRUD, `bind_sandbox`/`remove_sandbox_binding`, cross-class
exclusivity, the merged `list_bindings`/`BindingInfo`); the two
pre-commits (the spawner's kind-agnostic `NixServiceDefn` auto mode
in `fxtr_sandboxing`; the executor's shared escape-classification
rule); and chunk 3 (**serving** — `bindings.BindingHost`, the
launcher seam, the connect-time funnel, `shutdown_sandbox`, the
task-side module split; the fxtr_sandboxing scope-discipline root
fix, `plans/phase6_sandboxing/chunk3post1_sandboxing_scope_fix.md`);
and the physical task-side package split into the `task_package/`
dist (`plans/phase6_sandboxing/chunk3post3_package_split.md`).
Remaining: chunk 4 (the bindings file in the at-rest export) and
chunk 5 (monitoring/dashboard).

## Layout

This project ships **two distributions**. The task-side modules live in a
nested sub-dist so a sandbox guest can install them alone
(`task_package/` → `fxtr-experiment-task`, imported as
`fxtr_experiment_task`; see its own `CODE_OVERVIEW.md`); the runner dist
is this directory and depends on it with `[frames]`. Imports run runner →
task only — structurally, since a guest installs the task dist alone.
The deep module detail for both sides is below.

```
task_package/                # THE TASK-SIDE DIST (fxtr-experiment-task) — see its CODE_OVERVIEW.md
└── fxtr_experiment_task/
    ├── _ns.py               # the project's namespace UUID (leaf; the runner imports it too)
    ├── model.py             # canonical data model: positional axes/coords/paths, slice templates,
    │                        #   grid configs, generations, linkage + generation records,
    │                        #   the frame schema law (validate_frame_schema)
    ├── errors.py            # error taxonomy + the error-poisoning contract (module docstring)
    ├── views.py             # user-facing view calculus: named-axis views, pure rewrites, effectful ops
    ├── view_spec.py         # runtime view specs + canonicalization; docstring = the concept reference
    ├── task_api.py          # task/step fn contracts (plain protocols over views)
    ├── task_adapter.py      # @task_fn adapter + output normalization (the guest entry point)
    ├── runner_api.py        # task↔runner wire model (ViewToken/ViewGrant/ViewSel) + capability protos
    ├── view_runtime.py      # task-side view runtime: shadows, backend seam, step context, @step_fn
    └── tables.py            # the frame runtime: polars.DataFrame + all conversions ([frames] extra)
fxtr_experiment/             # THE RUNNER DIST (fxtr-experiment-task[frames] + fxtr-sandbox-types
│                            #   + fxtr-sandboxing — only runners install this)
├── __init__.py              # empty
├── user_api.py              # THE user capability protocol + its result dataclasses (the wire surface)
├── watch_api.py             # watch event schemas: typed kind-discriminated unions per watch family
├── encoding.py              # canonical identity-bearing encodings (docstring = what lives where)
├── frame_encoding.py        # canonical frame-value entity: the dataframe identity spec (leaf)
├── ops_engine.py            # per-generation record-checked ops core + durable steps/checkpoints
├── task_service.py          # the runner-side service boundary: the run-capability wire skin
├── bindings.py              # BindingHost — the one binding authority + serving funnel (both classes)
├── sandbox_launcher.py      # the launcher seam's real impl (the sole fxtr_sandboxing importer)
├── grounding.py             # the intent-axis LFP evaluator + stamps (docstring = correctness argument)
├── lint.py                  # the structural lint sweep (the implementation half of session.lint)
├── introspect.py            # dashboard introspection query bodies (summarize / reports / observers / grid graph)
├── change_hints.py          # ambient change-hint accumulator (internal; feeds the watch engine)
├── run_log.py               # runtime-only per-cell run-log buffers (executor-owned; never persisted)
├── watch_engine.py          # per-mount recompute-and-diff watch mirrors over the hint stream
├── currency.py              # currency evaluation (fresh / stale / unsettled) + verdict stamping
├── executor.py              # demand policy, run orchestration, the always-on safety pass
├── user_service.py          # capability impl, Workflow wrapper, open_local bootstrap
├── spec_registry.py         # runner-side token↔spec registry + per-run rule-grant minting
├── state_store.py           # WorkflowSession: the two-file durable session + all derived maintenance
├── storage.py               # workflow state-db sqlite DDL (docstring = the schema writeup)
├── export.py                # the at-rest export writer (git-repo layout)
└── importer.py              # the at-rest import loader (destructive, empty-or-force)
frontend/                    # the dashboard renderer package — see frontend/LOCAL_CODE_OVERVIEW.md
plans/                       # per-phase plans (phase0…phase4); historical rationale lives here
notes/                       # design docs; design_overview.md is the root source of truth
tests/                       # pytest suite (below)
```

## Module details

**Data model and addressing** (`model.py`, `encoding.py`): storage
addresses cells by ordered flattened positions; axis names are
per-view local vocabulary. `model.py` holds the canonical types —
axis specs, stored slice templates (`Param`, output selectors), rule
input bindings, grid configs (dataframe schemas, named task outputs),
generations, linkage records, and the at-rest `GenerationRecord`.
`model.py` also carries the frame schema law
(`validate_frame_schema`), which sits with the schema types because its
consumers straddle the task/runner boundary. `encoding.py` owns every
identity-bearing encoding: storage identity
text (key-paths, flattened coords, the grid-key charset law), entity
forms of configs/templates (canonical CBOR;
ids = fingerprints), step payloads, and the canonically-ordered
generation-record entity. `frame_encoding.py` owns the dataframe
*value* identity (the canonical sorted-rows frame entity and its
scalar rules; design: `notes/dataframes.md`) plus
`canonical_frame_from_ipc`, the one funnel every identity-minting path
shares (cell writes on both capabilities, the executor's frame-output
canonicalization) — host-side by doctrine, so it lives with the
identity spec rather than in the task-side runtime. `tables.py` is that
frame *runtime*: relational data moves through the calculus as a
`polars.DataFrame` (no wrapper type), and this module owns the exact
one-to-one column-kind↔dtype mapping and every conversion around it —
`make_frame`, IPC-stream ⇆ frame, frame → canonical rows, and the
write-boundary `validate_and_align` (names by set, exact dtypes, null
legality). It is the sole module
importing polars eagerly; the core keeps polars `TYPE_CHECKING`-only
(frame-op call sites import it lazily). Frame cell ops run end to
end: the wire carries arrow-IPC stream bytes
(`runner_api.FrameWireType`) in both directions, frame entity ids
never cross it, and reads serve canonical row order from the
session's projection. `gather` is the batched form — one wire op
(positionally-named key table in, one host-stitched stream out;
`tables.py` owns the key/stitch/join mechanics), decomposing into
per-cell observations with request edges committed up front and
misses demanded concurrently.

**Views and the wire** (`views.py`, `view_spec.py`, `task_api.py`,
`runner_api.py`, `view_runtime.py`, `spec_registry.py`): the
construction API is a calculus of capability rewrites — pure local
rewrites are free; only effectful ops enter the record. `views.py` is
the user-facing surface (data/dataframe/handle/task/checkpoint/
subtree/coord views, `Table` + `gather`); `view_spec.py` the symbolic
spec family, the pure rewrite fns, `canonicalize_open_task`, and the
reverse direction (stored rules → the specs a run grants).
`task_api.py` defines task/step fns as plain protocols (any async
`(ctx, /, **args)` callable); the author decorators ARE the service
adapters (`@task_fn` in `task_service.py`, `@step_fn` in
`view_runtime.py`). The wire (`runner_api.py`) is positional and
token-based — absolute paths never cross a task boundary;
`spec_registry.py` translates wire selections back to true specs
runner-side and mints one run's grants from the stored rule.

**Execution and bindings** (`ops_engine.py`, `executor.py`,
`task_service.py`, `task_adapter.py`, `bindings.py`,
`sandbox_launcher.py`, `errors.py`; the binding design:
`notes/sandboxed_bindings.md`): `ops_engine.py` is the per-generation
record-checked ops core — spec resolution, the universal record-check
(hit/miss/mismatch), read-xor-write, commitment-time request edges,
the request-obligation wrapper, and `run_step` + the step capability
(checkpoint record-check; poisoned invocations withhold entries).
`executor.py` owns the demand policy (strict currency gate + the
grounding serve gate), generation minting, per-run scopes, terminal
status from the failure set (one shared escape-classification rule:
a runner-side typed `CellFaultError` escaping a run *or* the pass ⇒
fault-class + interrupted), and the always-on safety pass
(frozen-engine replay gating completion). `task_service.py` is the
runner-side service boundary (the run-capability wire skin);
`fxtr_experiment_task.task_adapter` is its **task-side** half —
`@task_fn` / `TaskFnAdapter` + output normalization, the entry point a
sandbox guest imports, and hence the reason the task dist exists at all
(its module docstring carries the boundary it must keep — above all
polars laziness, the half packaging cannot enforce). `bindings.py` is
`BindingHost`, the runner's one binding authority: the ephemeral
entries (connector + pinned deps + config, release capabilities), the
durable-record consultation (records stay session-owned), the sandbox
process cache (per binding, in-flight dedup, runtime-only launch
states) and release-and-forget `shutdown_sandbox`, the bindings
change hint, and the one serving funnel every spawn goes through
(executor prologue — both runs — and step `connect`): look up either
class, re-read + pin-check each dependency (unrecorded), spawn deps
recursively (explicit in-progress set = the cycle check), assemble
the uniform `fxtr_sandbox_types.BindingConnectRequest`, connect. The
launcher seam (`SandboxLauncherProto`, defined beside the host) keeps
the funnel fake-testable; `sandbox_launcher.py` is the real impl —
the package's sole `fxtr_sandboxing` importer, loaded lazily by
`open_local(sandbox_repo=…)`: a lazy
per-runner environment, holder tasks per process, spawns via the
kind-agnostic `NixServiceDefn`, per-connect leases so shutdown never
cuts in-flight guests. Connect-time failures are fault-class
(`SandboxRepoUnconfiguredError`, `BindingDependencyFaultError`,
`BindingCycleError`, `SandboxLaunchError` — full nix stderr).
`errors.py`'s docstring is the poisoning contract: workflow errors
persist at raise time and fail the generation even if task code
catches them; nothing that crossed a wire is trusted for system state
(`typed_wire_call` reconstructs typed exceptions for callers as
ergonomics only).

**Derived state** (`currency.py`, `grounding.py`, `lint.py`):
currency verdicts and grounding verdicts are both evaluated on demand
and cached as stamps in the state db — marking (stamp clearing up the
observer DAG / justification component) happens inside every mutating
transaction, and the stamps are the only memo. `currency.py` runs the
three-valued well-founded recursion (cycles park at unsettled);
`grounding.py` the wanted/would_start/grounded LFP (its docstring
carries the invariants and per-mutation obligations); `lint.py`
enumerates the one-snapshot structural inventory behind the
capability's `lint`.

**The session and persistence** (`state_store.py`, `storage.py`,
`export.py`, `importer.py`): two sqlite files side by side — the
state db (schema writeup in `storage.py`'s docstring) and the fxtr
entity store, under the **liveness invariant** (an id is recorded
only after its closure is persisted). The session also owns the
**frame projection** (`FrameProjection` + `frame_bytes`): the
in-memory bytes-budget LRU serving frame ids as arrow-IPC bytes,
rebuilt on miss from the canonical entity by a trusting decode (pure
cache — never exported; a persistent variant is a deferred drop-in
behind the same seam). `WorkflowSession` owns all
maintenance: every mutating body declares its "Marks:" /
"Grounding marks:" / "Events:" lines (the greppable audit trail for
stamp clearing and change-hint emission), served ids are re-attached
per call site (`ensure_loadable`), terminal generations get their
record entity at flip time, and startup flips `running` →
`interrupted`. It also owns the **durable sandboxed-binding records**
(`sandbox_bindings` + `sandbox_binding_deps`; CRUD + the bind-time pin
reads shared with the ephemeral class via `pin_dependency_cells`, and
the funnel's `get_sandbox_binding` lookup) — not-identity state (no
marks; a `bindings` hint + event row), served by `bindings.BindingHost`. `export.py` / `importer.py` implement the at-rest
state as (git repo + entity store) per `notes/at_rest_format.md` —
deterministic sorted TOML/TSV trees; import is destructive,
empty-or-force, rebuilding derived tables with zero stamps.

**The user surface and monitoring** (`user_api.py`, `watch_api.py`,
`user_service.py`, `introspect.py`, `change_hints.py`, `run_log.py`,
`watch_engine.py`): `user_api.py` declares the single user capability
— reads under the demand policy, user-sourced writes/opens, the
sourcing CRUD + targets, clear/delete, attach (incl. read-only
internals browsing), the value-free presence queries, `inspect_cell`
(content WITH verdicts), the monitoring introspection (`summarize`,
`inspect_generation`, `list_observers`, `list_bindings` — both binding
classes, uniform deps/config + the sandbox launch state —
`shutdown_sandbox`, `grid_graph` —
the plate view's id-free relational overlay of edges + input grants +
axis correspondences, `notes/plate_view.md`), `lint`,
export/import, and the **watch subscriptions** (`watch_workflow` /
`watch_cells` / `watch_cell` / `watch_graph` — live diffed mirrors;
design: `notes/event_system.md`; the delivery + teardown contracts are on
the methods, the typed event unions in `watch_api.py`).
`user_service.py` implements it (no op record, interactive
exceptions, the same gates as task reads), plus the `Workflow`
convenience wrapper and the `open_local` bootstrap. `introspect.py`
holds the introspection query bodies; `change_hints.py` the
ContextVar'd hint collector the session installs around each op and
flushes post-commit (internal vocabulary — hints never cross the
wire); `watch_engine.py` the per-mount engine that consumes them —
per watch it recomputes through the capability's own query methods,
diffs against the last-sent state, and publishes keyed patches over a
queue-mode `SubscriberHub`, with grants for watched grids minted
through the same walk as `attach`; `run_log.py` the executor-owned
runtime-only per-cell log buffers (step `log()` + engine lifecycle
notes) the cell watch streams event-natively.

**The dashboard** (`frontend/`): the in-repo renderer package
(workstation FullPanel over the capability, the frame-entity
table renderer — dataframe cells render by loading the
self-describing frame entity through ordinary entity access, no
frame-specific wire surface — and the stored-config entity
renderers). Nix-packaged as one dispatch module by the repo lib
(`nix/lib/experimentRenderers.nix`), merged into the demo workdir
preset by reference. Everything about editing
it — layout, the wire-dialect mirror conventions and the live
round-trip harness, the grant-cache/token-pinning state conventions,
the mutation action log, the dev loop — is
`frontend/LOCAL_CODE_OVERVIEW.md`. Its live harness is
`examples/fxtr_experiment_demo` (persistent runners + scenario
drivers hosted in a workstation: the system-probing pipeline
scenarios plus an LLM-backed behavior-research scenario).

## Tests

`tests/` is one pytest suite covering each layer white-box and the
whole system end to end: encodings and the state-db schema; the frame
runtime (dtype mapping, IPC/rows conversions, write-boundary
validation, and the duckdb/arrow interop smoke test); the frame cell
ops (engine write/read multiset semantics, frame outputs through the
executor incl. the safety pass's canonicalized output comparison
under permuted replay order, per-output staleness on frame ids, the
user surface with sourcing/adopt/`demand_table`, output-kind guards,
projection rebuild across reopen and the at-rest round trip, and the
projection LRU); gather (per-cell record integration and replay
hits, fault-and-resume, the degenerate shapes, the local-join error
surface, the MH-aggregate scenario through the executor incl. the
safety pass, the concurrent-demand rendezvous proof, fan-in dedup,
and the user twin's gates); the
canonicalizer and view runtime; the ops engine, steps, and
boundary/poisoning; executor e2e incl. cancel/crash resume and the
safety-pass regressions; currency verdicts + the stamp lifecycle;
grounding (predicates, marking, gates, the lint suites); the
axis-semantics scenario suites (regular + irregular shapes, coord
views, read-sliced subtree windows); the user surface incl. targets,
sourcing/adoption, and an RPC smoke over a real wire; the binding
subsystem — the durable record CRUD + capability surface
(`test_sandbox_bindings.py`), the whole serving funnel over a fake
launcher (`test_sandbox_serving.py`: both consumption paths, pins,
cycles, dedup, launch states, release-and-forget shutdown, ephemeral
uniformity), the launcher scope-context regression
(`test_sandbox_launcher_scopes.py`: the real launcher over a fake,
hazard-preserving sandbox environment must tear down when driven from
inside a service dispatch — no nix), and the gated real-sandbox e2e
(`test_sandbox_e2e.py`, `nix_runtime`-marked: a wasm-family guest
through step-connect incl. in-flight-survives-shutdown — one fixture
built as both eryx and pyodide kinds, swept over both runtimes — and
a container guest serving a real task fn through the executor, over
the workdir fixture templates in `tests/_workdir_templates/`);
flip-time
generation records and at-rest export/import round trips; the
monitoring introspection and the watch subscriptions (snapshot/diff
delivery, tombstones, the run-log stream, subscriber lifecycle); and the
grid-graph overlay (the pure structure builder, edge aggregation over a
run, and the `watch_graph` delivery).
`boundary.py` is a shared one-run helper.

```
uv run pytest            # from projects/fxtr_experiment
uv run pyright           # pyright-clean, gated in pre-commit
```

Both run from this directory and cover **both** dists: the task package
is installed editable into this one venv and is in the pyright `include`
list, so there is no second venv and no second pre-commit hook.

Frontend checks run from the repo root (`pnpm exec vitest run
projects/fxtr_experiment/frontend`, plus the root tsc/biome) — see
`frontend/LOCAL_CODE_OVERVIEW.md`.
