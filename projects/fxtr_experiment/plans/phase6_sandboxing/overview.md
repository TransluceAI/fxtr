# Phase 6 — sandboxed bindings

Goal: make sandboxed bindings real per the standing design note
`notes/sandboxed_bindings.md` — the durable binding record (nix defn +
pinned dependency map + JSON config per handle key), the uniform
`BindingConnectRequest` contract shared by external connectors and
sandbox guest entrypoints, serving through `fxtr_sandboxing` (per-runner
environment, per-binding process cache, eager pinned-dependency
resolution), the runner repo configuration, and the exported TOML
bindings file. Design rationale: `discussion_log.md` (this directory).

## Chunks

Each chunk lands with tests, a CODE_OVERVIEW update, and design-doc
updates as decisions become current state (in particular, retiring the
phase-6 TODO in `CODE_OVERVIEW.md` when the last chunk lands); details
and deviations live in the chunk's own file. Chunk 1 is scoped to
implementation-ready; chunks 2–5 are pre-scoping records — settled
constraints plus the open questions that must be answered at chunk
start, before implementation steps are written.

- [x] [chunk1_connect_request.md](chunk1_connect_request.md) — **the
      request contract** (the
      standalone setup commit): `BindingConnectRequest` in
      `fxtr_sandbox_types` (key + JSON config + named dependency refs;
      documented as the standard interface fxtr_experiment consumers
      implement against — settle FxtrID vs EntityRef for `key` here,
      including arena coverage of the key's closure at spawn);
      `ConnectorProto.connect()` → `connect(request)`;
      `_SingletonConnector` / `wrap_singleton` updated (request
      ignored); `BindingRegistry.spawn` builds key-only requests
      (`config=None`, `handles={}`); `fxtr_experiment` gains the
      `fxtr-sandbox-types` dependency; all implementors updated —
      the demo drivers (`examples/fxtr_experiment_demo`:
      `experiment_runner.py`, `behavior_runner.py`,
      `experiment_demo_helper_service.py`) and the test-suite
      connectors.
- [x] [chunk2_durable_records.md](chunk2_durable_records.md) — **durable records + capability
      surface**: the state-db sandboxed-bindings table + session CRUD
      (Events:/hints lines; no currency marks — bindings are not
      identity); key-closure persistence at bind (liveness);
      `bind_sandbox` (pin minting: dependency cells read in
      declaration order, populated required, current key id pinned) /
      `remove_sandbox_binding` / `set_sandbox_repo` (ephemeral runner
      field, wire-settable); cross-class exclusivity (either
      registration order rejected second); the new error types;
      `list_bindings` / `BindingInfo` grow the class discriminator +
      repo-configured state. Records are inert this chunk (resolution
      of a sandbox-bound key faults not-ready until chunk 3).
- [x] [chunk3pre1_auto_kind_spawn.md](chunk3pre1_auto_kind_spawn.md)
      — **pre-commit (fxtr_sandboxing): kind-agnostic spawn**: the
      `NixServiceDefn` triple entity + the auto-dispatch spawn arm
      (eval the attr's `kind`, per-env kind cache), so chunk 3 spawns
      from the stored triple with no kind knowledge and no `kind`
      column.
- [x] [chunk3pre2_escape_classification.md](chunk3pre2_escape_classification.md)
      — **pre-commit: one escape-classification rule**: the run path's
      fault-vs-error classification of escaped exceptions factored
      and applied to the safety pass too (the pass re-invokes the
      task fn, so its setup faults are interruptions, not
      nondeterminism) — the rule every chunk-3 connect-time fault
      rides at both executor spawn sites.
- [x] [chunk3_serving.md](chunk3_serving.md) — **serving through fxtr_sandboxing**:
      the task-side module split (`task_adapter.py` + the
      import-boundary test); `BindingRegistry` dissolves into
      `BindingHost` (`bindings.py`: ephemeral entries with pins +
      config, the uniform connect funnel with eager recursive
      dependency resolution + cycle check, the per-binding process
      cache with in-flight dedup and launch states,
      `shutdown_sandbox` with release-and-forget semantics); the
      launcher seam (`sandbox_launcher.py`, the sole
      `fxtr_sandboxing` importer; lazy per-runner environment;
      `open_local(sandbox_cache_dir=…)`); the connect-time
      fault-class errors (repo-unconfigured, dependency pin faults,
      cycles, launch failures with full stderr); ephemeral
      `bind_handle` grows the same `deps` + `config`; v0 runtime
      split (task fns: container; plain step-connected services: any
      runtime); fake-launcher unit coverage of the whole funnel plus
      gated e2e — a wasm plain-service fixture through step-connect
      and a container task-fn fixture through the executor.
- [x] [chunk3post1_sandboxing_scope_fix.md](chunk3post1_sandboxing_scope_fix.md)
      — **followup commit: fix the scope-discipline root in
      fxtr_sandboxing** (the chunk-3 deadlock's enabler:
      `create_sandbox_environment`'s non-isolated host scope; plus
      the package's implicit-scope audit and the launcher-side
      context cleanup). Own planning round; lands before chunk 4.
- [x] [chunk3post2_scope_safeguards.md](chunk3post2_scope_safeguards.md)
      — **PROPOSAL (discussion, then per-item plans)**: the
      safeguard menu distilled from the deadlock hunt —
      explain-drain / drain-watchdog tooling, debug-mode hazard
      detection, scope-discipline doc additions, a debugging-tips
      section. Mostly v2-side once scoped.
- [x] [chunk3post3_package_split.md](chunk3post3_package_split.md) —
      **the physical task-side package split**: the
      `fxtr-experiment-task` dist (`task_package/`) with polars behind
      a `[frames]` extra, the runner depending on it, and the
      `[sandbox]` extra retired now that only runners install the
      runner dist. Landed in four commits — boundary prep, the split,
      dropping the import-boundary test, the guest-facing flip.
- [ ] [chunk4_export_import.md](chunk4_export_import.md) — **the bindings file**: the sorted
      deterministic TOML schema (coords via the at-rest encoding
      conventions; pinned key ids as store references), export writer
      + import verification/restore (inert until repo configuration),
      round-trip tests, and the at-rest format note's file-schema
      section.
- [ ] [chunk5_monitoring.md](chunk5_monitoring.md) — **monitoring + dashboard**: the bindings
      watch family carries both classes, launch state, and the repo
      state; dashboard bindings panel (class badge, launch state,
      shutdown affordance, pin-mismatch surfacing); demo harness
      scenario exercising a sandboxed binding end to end.
