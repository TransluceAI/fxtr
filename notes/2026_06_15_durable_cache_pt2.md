# Durable run-graph cache, part 2 — fast, resumable, syncable cold build

Status: plan / design. Author: pairing session 2026-06-15. Follows `2026_06_15_durable_cache.md`.

## Problem

Part 1 shipped the durable co-located cache (`_rg/` rows in `active.sqlite`) and lazy startup
(Stages 0–2). Steady-state startup is now sub-linear. **But the one-time cold build was left
unoptimized** — `ActiveRunManager._eager_build_all` (+ `_rebuild_run_graph_cache`) still:

- loads the **whole** graph into memory before writing anything;
- does the per-node dependency work **single-threaded** (the "Adding runs" loop);
- loads `run.provenance` per node purely to *validate* (never stored);
- writes the cache in **one final transaction** — so a crash loses everything;
- retains all provenances + all `RunNode`s + runs simultaneously → tens of GB, OOM risk.

On a **~4M-entry** runset this cold build takes **8+ hours** and is **not recoverable**: a crash
restarts from zero. The cold path runs often because in-progress work / external runset overwrites
invalidate the cache. This is the build that produces the cache in the first place; lazy startup
can't help until it has run to completion once.

## Goals

1. **Faster.** Parallelize the expensive entity loads (now embarrassingly parallel); stop loading
   provenance during the build.
2. **Incremental / resumable.** A crash (or OOM, or kill) in either stage loses at most one batch;
   a rerun continues from where it stopped and never redoes the expensive work already captured.
3. **Syncable, into a separate file.** Build into a **new** file by copying from a read-only source
   runset + entity store, so other jobs keep using the original while the (hours-long) build runs.
   Re-running picks up runs **added** to the source since the last pass, until the target catches
   up and you cut over.

Constraint (unchanged): **do not change the canonical on-disk formats** — the CID-addressed CBOR
entity store, and `active.sqlite` (`task_cid → run_cid` runset rows + `_rg/`-prefixed cache rows).
The destination file we build is byte-compatible with `active.sqlite`, so after cutover it simply
*is* the new `active.sqlite` and the existing runtime / lazy-load paths take over unchanged.

## Confirmed assumptions (with Daniel)

- **Monotonic source during a build.** While converting / re-running, the source runset is only
  **added to** — never has existing entries removed or re-pointed. So an existing
  `(task_cid → run_cid)` never changes once seen: "already done" = **presence** in the destination,
  no `run_cid`-recheck, no change handling, and **no deletion-diff pass**. (If removals/re-runs
  during conversion are ever needed, add a cid-only diff that deletes missing cids and surgically
  removes them from their deps' `dependent_cids`; the format supports it. Out of scope.)
- **In-place = single writer.** When run in-place (destination swapped over the active runset at the
  end), nothing else is writing the active runset during the build — so the final swap is safe with
  no merge/loop. Offline mode reads a possibly-live source **read-only** and never swaps (operator
  cuts over).
- **`dependency_cids` of a built node are stable.** Task-handle deps come from the immutable
  `task.function`; the only run-derived deps (`WorkflowResultHandle` chains) belong to a node we
  never re-process. So a node's deps are computed once.

## Why provenance can be dropped during the build

`run.provenance` is loaded in `_check_completed_run_deps` **only to validate** that a run's
dependency provenance matches the active set. It is **never stored** in the cache record
(`_node_to_cache_record`: `run`, `dependency_cids`, `dependent_cids`, `workflow_contains`,
`workflow_next` — no provenance), and it does **not** affect future runs: a live session re-loads
and re-validates provenance itself in `add_completed_run` / `fulfill_reservation`. So skipping it
during the cold build only skips re-verifying a runset the system already wrote (and which is
authoritative). The strict check still runs at runtime. Dropping it removes ~one entity load (+
child loads) per node and the entire single-threaded "Adding runs" phase.

## Design: three stores, two stages

Three separate sqlite stores, only the last two written:

- **source** (read-only): the runset (`task_cid → run_cid`). In-place = the config's
  `active.sqlite`; offline = `--source`. We read only its runset rows (skip any `_rg/` rows).
- **staging** (read-write, temp): one light-ish record per node — the expensive work, checkpointed.
  Deleted at the end.
- **dest** (read-write): a fresh, byte-compatible `active.sqlite` — copied runset rows + `_rg/`
  cache rows. Offline = `--target`; in-place = a temp file swapped over source at the end.

The core operation is an **idempotent reconciliation**: make dest contain a runset row + an `_rg/`
cache row for every source entry, deriving records from the entity store. Run once → full
migration; rerun → only the newly-added source entries; crash anytime → continue from current
store contents. **The stores themselves are the progress tracker** — no completion marker.

### Why three stores / two stages (the key reasoning)

Two forces are in tension. (a) The expensive part — loading each node's `run` + `task` (+ `run_data`
for workflows; `task.function` is the fat recursive plan) — must be **parallel** (it already is
today; serializing it would be ~32× slower). (b) The reverse index (`dependent_cids`) is built
correctly and memory-cheaply by a **dependency-ordered walk** with surgical updates — which is
inherently serial.

The resolution: **put parallelism where the cost is, and ordering where it's cheap.**

- **Stage 1 (source → staging): parallel, expensive, resumable.** Per-node extraction is
  *independent* (a worker touches only its own node — see below), so it is embarrassingly parallel.
  It touches **only staging** — never source or dest — so it never contends with other jobs on the
  live file. This is where the 8 hours get parallelized and checkpointed.
- **Stage 2 (staging → dest): serial DAG-walk, local, cheap, resumable.** No entity loads
  (everything is in staging), so serial is fine — it's just local sqlite, minutes at 4M — and
  serial makes the surgical reverse-index trivially correct (no concurrency). **No in-memory
  invert** of the reverse index: it is built incrementally in dest rows during the walk, so there is
  no ~GB map, regardless of runset size.

Separation also keeps the "done" signal honest: dest rows are written **only in stage 2, only
complete** (run + deps + `dependent_cids` + workflow info), so "dest row present = fully integrated"
holds, which is what makes resume + the count check trustworthy.

### Stage 1 — extract (parallel)

For each source `(task_cid, run_cid)` **not already in staging**: load `run`, `task = run.task`,
and (for a `Workflow`) `run.run_data`; **no provenance**. Compute, touching no other node:

- `direct_dep_cids`: the refs from `TaskResultHandle(task_ref)` / `WorkflowStepResultHandle(
  workflow_ref)` in `plan_deps(task.function, recursive=True)`.
- `wf_result_refs`: the **unresolved** `WorkflowResultHandle(workflow_ref)` targets (their chains
  are resolved in stage 2, which has all of staging — this is what keeps the worker independent).
- `workflow_contains` / `workflow_next`: for a `Workflow`, from `run.run_data` (reuse
  `_get_workflow_contains_and_next`); else `None`.

Write the staging record `{run, run_cid, direct_dep_cids, wf_result_refs, workflow_contains,
workflow_next}`. (Staging holds the thin `run` so stage 2 needs no entity-store reload — the run is
written twice, staging then dest, both local/cheap. The alternative, storing only `run_cid` and
reloading in stage 2, would re-serialize the serial DAG-walk's loads — rejected.)

**Executor**: `concurrent.futures` with a swappable executor — **threads by default**, **processes
opt-in**. Workers are independent, so a `ProcessPoolExecutor` (per-process loader built in an
`initializer` from the picklable locator; workers return records; the parent is the single staging
writer) can side-step the GIL on `ipld_entity_data_to_entity` + the `plan_deps` walk. Caveat: in
process mode the thin `run` objects are pickled back across IPC (~4M); if that limits the speedup,
the follow-up is per-worker staging shards merged at the end (no IPC of runs) — noted, not built
yet. The parent writes staging in **batches** (commit every ~20k), bounding memory and crash-redo;
resumability comes from skip-present, not batching.

### Stage 2 — DAG-walk into dest (serial, local)

Iterate staging cids **not already in dest**; for each, an **iterative depth-first post-order**
walk:

- A node's full `dependency_cids` = `direct_dep_cids` ∪ (each `wf_result_ref` resolved by walking
  the staged `workflow_next` chain), all required to be present in staging.
- Ensure every dependency is in dest first (recurse into unbuilt deps — we can always find them by
  cid). Then the node is ready.
- **Batched atomic flush** (~20k ready nodes): one transaction that (1) writes each node's dest
  runset row + complete `_rg/` record (`dependent_cids = []`), then (2) applies the reverse-appends
  **grouped per parent** (read each affected parent's dest row once, extend its `dependent_cids`
  with all new dependents from this batch, write once), then commits. Own-rows first so a parent in
  the same batch already has a row to append to; appends idempotent (`if child not in
  dependent_cids`).

Because deps are built before dependents, a new node always starts `dependent_cids = []` and is
appended to by its dependents later; "dest row present = node fully integrated". Resume reseeds the
"built" set from dest's `_rg/` rows. Memory: the DFS frontier (bounded by **chain depth**, short in
practice) + a batch buffer + a built-cid set (offloadable to dest-presence checks).

### Finish

Delete staging. **In-place**: `os.replace(dest, source)` (single-writer assumption), then the
manager flips to lazy mode (`_fully_loaded = False`, `nodes = {}`). **Offline**: leave dest, report
counts; operator cuts over.

### Unbuildable nodes

A runset entry whose `run`/`task`/`run_data` won't load is **corruption** (the invariant is that
every entry's entities are present in the content-addressed store), so in a consistent system this
never fires. We do **not** silently paper over it.

- **Default: fail-loud abort.** Stage 1 raises with the offending cid and the underlying failure.
  Because the build is resumable, the operator repairs the store (or removes the bad run via normal
  tooling) and reruns — already-staged/built rows are skipped. We never reach a "looks complete but
  isn't" state, which also kills the latent eternal-rebuild bug in the current eager path (a
  silently-skipped node leaves `cache_count < runset_count` forever → full rebuild every startup).
- **We never edge-drop** — never keep a node `Y` while dropping a dependency `X` it points at (that
  violates runset consistency and just relocates the failure). The only consistent omission is to
  also omit `X`'s entire transitive **dependent closure**, keeping the kept set dependency-closed.
- **Opt-in `--skip-unbuildable`** (offline only): stage 1 records the failed cids (does not stage
  them); stage 2's DAG-walk then **propagates** the failure — any node with a dep missing from
  staging is itself skipped — which naturally drops the whole transitive dependent closure (no
  ordering hole: post-order attempts deps before the node, even across roots). The skipped set is
  reported; dest ends a valid, dependency-closed subset (only kept nodes' runset rows are copied).

## Operational model & cutover

Inputs: read-only **source** runset + read-only **entity store**; read-write **staging** (temp) and
**dest**. We snapshot the source runset at the start of a pass; runs added afterward are picked up
on the next pass. **Cutover**: run until `dest runset == source runset` (a final pass after the
source quiesces guarantees it), then move/point dest into place. The tool takes explicit
`--source` / `--target`; the operator controls the swap (in-place mode swaps automatically under the
single-writer assumption).

## Code shape

- New standalone **`RunGraphCacheBuilder`** (in `session_manager.py`, near `ActiveRunManager`,
  sharing the `_rg/` key + record conventions): constructed with source runset + loader + staging
  path + dest path + options (`copy_runset`, `batch_size`, executor/threads-vs-processes,
  `skip_unbuildable`); `run() -> (built, skipped)`. Stage-1 worker is a module-level function
  (picklable for `ProcessPoolExecutor`); stage 2 is the serial DAG-walk.
- **In-place startup**: `ActiveRunManager._load_runset` cold path constructs the builder with
  source = active runset, staging = `<active>.staging`, dest = `<active>.rebuild`, runs it,
  `os.replace`s dest over the active path, then flips to lazy mode. Replaces and removes
  `_eager_build_all`, `_load_run_and_task`, and `_rebuild_run_graph_cache`.
- **Offline migration**: `scripts/migrate_runset_cache.py` is unified on `RunGraphCacheBuilder` and
  **always** builds `--source` → `--target` (both required; `+ --staging`, `--skip-unbuildable`,
  `--stage1-processes N`, `--cleanup-staging`). It never rewrites in place or swaps — to migrate in
  place you build to a separate target and swap it over the active store yourself once it has caught
  up. (The in-place *startup* path in `_cold_build` still builds + swaps automatically under the
  single-writer assumption; the script deliberately does not.)
- **Staging persistence**: `RunGraphCacheBuilder` has `keep_staging` (default `False` → delete on
  success, used by `_cold_build`). The script passes `keep_staging=True` so the staging checkpoint
  **survives between runs** — a later run then extracts only the *newly-added* source runs
  (incremental sync) instead of re-extracting everything. `--cleanup-staging` deletes it (use on the
  final run, or just `rm <target>.staging` when done syncing).
- `_topologically_sort_tasks` stays (still used elsewhere, ~line 2748).

## Tests

Behavioral regression tests in `tests/test_runset_reload_regression.py` should pass unchanged (cold
rebuild reconstructs the graph; workflows survive; drops cascade after reload; reverse index is the
inverse of forward; self-heal on corruption; legacy pickle migration).

White-box update: `test_incomplete_cache_triggers_eager_rebuild` currently asserts the post-rebuild
manager is **eager** (`_fully_loaded is True`, nodes resident). New contract: the rebuild populates
the cache and then serves **lazily** (`_fully_loaded is False`, `nodes` empty, cache repopulated to
match the runset, results resolve). Update assertions.

New tests:
- **Resumable, both stages**: interrupt after partial staging → rerun completes; interrupt after
  partial dest (staging complete) → rerun completes. Assert final dest is consistent
  (forward == inverse of reverse) and results resolve.
- **Sync / monotonic add**: build dest from source; add runs to source; rerun; assert only the new
  cids are processed and dest stays consistent.
- **Source → target copy**: build into a separate dest from a read-only source; source untouched; a
  manager opened on dest goes lazy and resolves results.
- **Unbuildable node**: missing entity → build **aborts** naming the cid (no skip, no edge-drop);
  rerun after the row is removed completes.
- **Opt-in cascade-skip**: with `--skip-unbuildable`, the unbuildable node and its **entire
  transitive dependent closure** are skipped together; assert no kept node references a skipped cid
  and the skipped set is reported.
- **Workflow-result-handle deps**: a node depending on a whole workflow's result rebuilds with the
  correct resolved chain in `dependency_cids` after reopen.

## Open / deferred

- Removals & re-runs during conversion (deletion-diff pass) — deferred per the monotonic assumption.
- Process-mode staging shards (avoid IPC of run objects) — only if process IPC limits the speedup.
- Graceful in-place degradation on corruption (tombstone `_rg/` rows) instead of fail-loud abort —
  noted, not planned unless we want startup to survive a corrupt entity.
