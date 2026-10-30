# Part 4 — slot deletion + dependency index (stub)

> Stub plan. To be scoped out in detail once parts 1–3 exist. Captures
> boundaries + what's involved.

Adds the ability to delete realization-map slots while preserving the coherence
invariant: you can never remove a result that a *retained* result observed.

## What's involved

- **Dependency index**: register a namespace indexer over `WorkflowTaskComplete`
  pins in the realization map that emits one row per `depends_on` entry —
  columns `(dep_path: str, dep_value_id)`, plus the depending pin's own
  path/key. Uses the real `IndexablePinningBackend` indexer machinery (not a pin
  scan), so it scales to large runsets.
  - **OQ-6** (the one remaining open question): confirm the path-expression
    indexers can emit *dict keys* (`depends_on` keys) as a column. If not,
    either extend the indexer path language or change `depends_on`'s
    representation to an indexable list of `{path, value}` pairs (cheap entity
    change). **Discuss with Daniel before implementing any fallback.**
- **`runner.clear_slots(pattern)`** (runner-level python method, not a
  task-grantable capability):
  1. Resolve `pattern` → target path set; error if any target has an in-flight
     job.
  2. Query the index for rows where `dep_path ∈ targets` and the depending pin
     ∉ targets → `SlotDeletionBlockedError` listing the dependents.
  3. Unpin all targets; append `SlotsDeleted` op-log event(s).

## Boundary

- Depends on part 3's `depends_on` being populated correctly.
- Deletion is realization-map-only; the op log is append-only (a `SlotsDeleted`
  event, never a removal). A future event-sourcing redesign (followup) could
  change this.

## Tests (sketch)

Delete a leaf slot (ok); delete a slot a retained slot depends on (blocked,
lists dependents); delete a whole dependent subtree together (ok); delete with
an in-flight job (error); index correctness over many pins.
