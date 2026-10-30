# Stage 15: Workflows — overview

A durable, trace-based task/workflow system on top of fxtr2, as sketched in
[sketch.md](sketch.md) and refined by
[design/proposed_change_sketch_2026_06_16.md](design/proposed_change_sketch_2026_06_16.md):
**tasks** (possibly-nondeterministic but observably pure computations, bound to
args) are run into named **slots** of a **realization map**, executed by **task
functions** which are stateless services spawned from content-addressed
**service handle** entities — either external/trusted or nix-built/sandboxed.
Operations on the realization map are recorded in a separate append-only **op
log**.

## Strategy / current state

This stage is being developed docs-first, so the markdown in this directory
always represents the full state of what we know, what we've discussed, and
what we plan to do next:

1. ✅ `sketch.md` — Daniel's original sketch (kept verbatim).
2. ✅ `design/proposed_change_sketch_2026_06_16.md` — Daniel's refinements
   (kept verbatim); folded into the docs below.
3. ✅ This overview + the two design docs under `design/` — fleshed-out
   design, recording decisions from Q&A rounds.
4. 🔄 `design/open_questions.md` — remaining questions, iterated with Daniel
   until the design is settled. Resolved answers get folded back into the
   design docs and the question marked resolved.
5. ⬜ `partN_*.md` implementation plans (see layout below) — written once the
   design is settled.
6. ⬜ Implementation, one part at a time, checking off plan steps as we go.

## Doc map

- [sketch.md](sketch.md) — original sketch (kept verbatim).
- [design/proposed_change_sketch_2026_06_16.md](design/proposed_change_sketch_2026_06_16.md)
  — Daniel's refinement sketch (kept verbatim).
- [design/workflow_runner.md](design/workflow_runner.md) — the workflow runner
  / slot manager: realization map + op log, run groups,
  TaskSlot/TaskStepContext/TaskFn surface (bare ServiceRefs), slot pin states,
  invocation entities + cache keys, result provenance, job/requester lifecycle,
  failure semantics, deletion + dependency index, op-log event entities.
- [design/stateless_service_provider.md](design/stateless_service_provider.md)
  — service handle entities, the spawner protocol + capability split, the basic
  external-only provider (`fxtr2.workflows.stateless_service_provider`), and the
  nix-sandboxed provider (`fxtr_sandboxing`).
- [design/open_questions.md](design/open_questions.md) — open design questions
  with proposals.
- [followups.md](followups.md) — tracked extensions and deferred work.
- Planned implementation plans:
  - `part1_types_and_basic_provider.md` — handle/protocol types + basic
    stateless service provider.
  - `part2_workflow_runner_core.md` — runner core (two namespaces, run groups,
    jobs/requesters, persistence + op log); no nesting, no deletion.
  - `part3_storage_args.md` — `TaskStorage` args + `step_ctx.subslot`,
    waiting/joining, child tracking, poisoning, slot-path cache keying.
  - `part4_storage_deletion.md` — slot deletion + dependency index.
  - `part5_sandboxed_service_provider.md` — nix-built hermetic provider.

## Conceptual model

- A **procedure** / **task function** is a self-contained stateless operation
  runnable with different arguments.
- A **task** is like a thunk: a procedure bound to args, cacheable into a slot.
- **Realization** is the act of getting an outcome from a task, by executing
  procedures and/or retrieving (sub)results from cache.
- The **realization map** is a (mutable, pointwise) map from slot → successful
  task result. It holds *only* successful `WorkflowTaskComplete` pins.
- The **op log** is an append-only history of operations on the realization
  map (run groups opened/closed, jobs launched, results recorded incl.
  failures, observations, deletions). Claims and failures are *not* in the
  realization map — they live in runner memory + the op log.
- A **run group** groups runs launched together, for op-log legibility only; it
  does not affect the realization map.
- A **service handle** is an entity identifying a stateless service: *built*
  stateless (nix/sandboxed) or *promised* stateless (external, trusted). Its
  identity determines caching.
- **Coherence invariant**: if the result in slot A observed the result in slot
  B, then B's result must remain in slot B for as long as A's remains in A.

## Key decisions (settled in Q&A with Daniel)

1. **Location.** `v2/fxtr2/workflows/`: `task_types.py` + `runner.py`, plus a
   `stateless_service_provider/` subpackage (handles, protocols, basic
   provider). The nix-aware provider goes into the existing
   `projects/fxtr_sandboxing`. The old `fxtr_workflow` project moved to
   `spikes/` and is ignored entirely.
2. **Two namespaces.** The runner owns a **realization map** (queryable
   namespace, successful results only, mutable pointwise) and an **op log**
   (append-only event history). v0: both `SQLiteFxtrNamespace`, usually two
   named namespaces in one SQLite session / backing db (no physical data
   duplication). TSV becomes an export format later; the op log may later seed
   an event-sourcing redesign.
3. **No durable claims / incomplete.** Claims and in-progress/failed state are
   in-memory only; failures are recorded as op-log events. The realization-map
   pin is empty or a single `WorkflowTaskComplete`.
4. **Bare ServiceRefs, no adapters.** `TaskStorage` is a
   `ServiceRef[TaskSlotProto]` and `TaskStepContext` is a
   `ServiceRef[TaskStepContextProto]`; `subslot` is an async protocol method
   minting a new runner-side mount. No `$fxtr.workflow` marker encoding.
5. **Run groups.** The runner and run groups are plain-python async CMs (not
   services): `async with runner.run_group(name, description) as group:` records
   open/close op-log events; `group.get_storage(path)` (nonempty path) returns
   the first service (a `TaskSlotProto` ref). Run groups are an op-log grouping
   device, recorded with each event (every event also carries the runner's
   `owner`); they don't touch the realization map. A top-level run is attributed
   to one run group (all storage args to it must share that group).
6. **Cache key.** The cache key is the FxtrID of a canonical
   `WorkflowTaskInvocation`. Entity args are kept as **full `EntityRef`s** (so
   their closures travel into the map and stay loadable) but with provenance
   parents **stripped** (key is provenance-blind; provenance plumbing is a
   followup). Slot-args contribute their **root-relative slot path**;
   non-slot live `ServiceRef`s are rejected.
7. **Result provenance.** Task functions return an `EntityRef`.
   `WorkflowTaskComplete.result` always stores a *provenance-layer* node:
   extract the prov half if the returned id has a `_provenance_parent`, else
   `u`-wrap a bare data node — keying on the prov *parent* sidecar, not the
   node's *type* tag (provenance data is a valid return). Mirrors
   `PreserveProvPersistenceImpl`. Retrieval returns the stored node verbatim; a
   higher-level API re-attaches it via `embed_as_provenance_parent`.
8. **Jobs vs requesters.** Jobs (one per active slot, run the fn once, own the
   slot) are separated from requesters (per-await tasks parked on a job). Many
   requesters can join one job; cancelling your own await doesn't cancel the
   job. Clean teardown waits for jobs; explicit cancellation writes
   interrupted/cancelled op-log events.
9. **Spawning.** The runner spawns per job; the provider owns service lifecycle
   and any caching (OQ-11). Task fns are authored as `TypedServiceImpl`
   subclasses (typed in the impl, untyped on the wire) — no dedicated authoring
   base needed.
10. **Sandboxed services support both runtimes** (wasm + container). The nix
    attrset schema is a *new* kind alongside the existing
    `NixWasmServiceDefn`/`NixContainerServiceDefn`; nix helpers live in root
    `nix/`. The guest seam reuses the existing `SandboxConnectProto` + guest
    adapter, with a `StatelessServiceConfig(config, bindings, spawner)`
    dataclass as the entrypoint arg.
11. **Dependency index.** Deletion's dependents check uses a real namespace
    indexer (`IndexablePinningBackend`) over the realization map.
12. **Provider, not manager.** The stateless-service piece is named
    "provider" throughout (`StatelessServiceProviderProto`,
    `BasicStatelessServiceProvider`, `SandboxedStatelessServiceProvider`).

## Sequencing

```
part1 (handle types + protocols + basic provider)
        │
part2 (runner core: two namespaces, run groups, slots, jobs/requesters,
        │            dedup, persist + op log; no nesting)
        │
part3 (TaskStorage args + step_ctx.subslot: waiting, child tracking,
        │                                   poisoning, slot cache keying)
        │
part4 (deletion + dependency index)
        │
part5 (sandboxed/hermetic service provider; nix/ helpers)   ◄─ usable system
        │
followups (provenance plumbing, replay, checkpointing, streaming output,
           CAS primitive, run-group cancellation, event-sourcing, …)
```

Parts 1–4 are fully testable inside `v2/` with pytest against the basic
(external-only) provider. Part 5 builds on `fxtr_sandboxing` and gets its own
integration tests there.
