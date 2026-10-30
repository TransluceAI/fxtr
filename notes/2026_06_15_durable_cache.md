# Durable, incremental run-graph cache

Status: plan / design. Author: pairing session 2026-06-15.

## Problem

`ActiveRunManager._load_runset` (`fxtr/api/session_manager.py`) reconstructs the in-memory
run graph (`self.nodes: dict[task_cid, RunNode]`) on session-manager startup. With runsets on
the order of **~4 million entries**, a cold load is painfully slow, and the cold case happens
**often** because in-progress work / crashes invalidate the existing cache frequently.

Goals:
- Cold restores should not be O(size of runset) in the expensive sense (entity loads + plan-dep
  walking); ultimately startup should be **constant/logarithmic** (Stage 2).
- The cache must be **self-maintaining**: if it doesn't exist, the first load builds it; from then
  on every mutation keeps it in sync, so we never pay full reconstruction again unless the cache
  is genuinely lost/corrupt.
- **Never compromise the consistency invariants** of the run graph.

Constraint: **do not change the on-disk format of the canonical data storage** — the
CID-addressed CBOR entity store, and the `active.sqlite` runset (`task_cid -> run_cid`). Adding
*new* auxiliary caches is fine.

We split this into stages:
- **Stage 0** — regression tests that pin current observable behavior across session reopen, so
  Stage 1/2 can refactor `_load_runset` and the graph representation safely.
- **Stage 1** — durable, incremental sidecar that stores the reconstructed graph (both edge
  directions). Still eager-loads the whole graph at startup, but is crash-durable and survives
  in-progress work. Fixes the "cache is usually missing" pain. Ships the hard-to-get-right
  persistence layer **once**, in a format that Stage 2 reuses verbatim.
- **Stage 2** — flip graph access to **lazy** (load-on-demand) + indexed membership, giving
  sub-linear startup. No on-disk format change; mostly mechanical given Stage 1's groundwork.

We only need **"Level 1" lazy** (load-on-demand, no eviction): once loaded, a node stays resident
for the session, so `RunNode`'s existing `id()`-keyed edge dicts and direct object references keep
working unchanged. We explicitly do **not** do the `id()`→`task_cid` refactor that bounded-memory
eviction ("Level 2") would require.

---

## Current state (branch b9c8f2de)

1. **Runset storage** = `runsets/active.sqlite`, a `sqlitedict` of `task_cid -> run_cid`
   (`api_lib._load_runset_from_sqlitedict`, `insert_into_active_runset`,
   `drop_from_active_runset`). Authoritative and already incremental. `build_runset`/`save_runset`
   on `ActiveRunManager` are **gone** on this branch.
2. **Threaded entity load + provenance-retention** during cold rebuild
   (`_load_runset(use_threading=True)`, `thread_map`).
3. **Monolithic pickle run-graph cache** = `<state_store>/_run_graph_cache.pickle`
   (`_save_run_graph_cache`, read at top of `_load_runset`). Pickles the whole `nodes` dict. This
   is what Stage 1 replaces.

### Why the cold rebuild is expensive

`core.Run` (core.py:5074) is *thin* — its fields are all `CID_Of[...]` (`task`, `provenance`,
`run_data`) plus a small `run_info`; loading a `Run` does **not** pull its subgraph. The cost is
the derived work done per run during rebuild:

- load `run.task` — the `Task`/`Workflow`, whose `.function` is a (fat, recursive) plan;
- `_check_completed_run_deps`: load `run.provenance` (`ImplProvenanceSet`, can be large) + walk
  `plan_dep_util.plan_deps(task.function, recursive=True)`;
- workflows: `_get_workflow_contains_and_next` loads `run.run_data` (`WorkflowState`) and walks
  every task in the step data;
- then `_topologically_sort_tasks` walks `plan_deps` again for the whole set.

The *output* of all that is three small pieces of derived structure per node: dependency CIDs,
`workflow_contains`, `workflow_next`. That is exactly what we cache.

### Why the existing pickle cache is fragile (the "missing cache" pain)

`_save_run_graph_cache`:
- written **only at clean shutdown** (`finally` of `SessionManager.create`, session_manager.py
  ~2576) — a crash/kill never writes it;
- **refuses and deletes** the cache when any reservation or workflow commitment is active (needs a
  fully quiescent graph) — so any in-progress session loses it;
- **monolithic** — one giant pickle, (de)serialized wholesale; at 4M nodes a multi-GB
  all-or-nothing blob.

### Consistency invariants we must preserve (both stages)

1. **At most one run/reservation per `task_cid`.**
2. **Runset consistency**: if a run is in the set and depends on R', then R' is in the set
   (extended to reservations). A node is never added before its dependencies exist.
3. **Bidirectional edges**: X has Y as a dependency ⟺ Y has X as a dependent
   (`RunNode` docstring).
4. **Provenance matching**: a completed run's provenance must match the run CIDs of its dependency
   nodes (`_check_completed_run_deps`).
5. **Worker tracking**: if node X has worker W, W tracks X; W removes itself on going stale.
6. **Workflow-commitment rules** (e.g. a step commitment is only fulfilled once its next step has
   its own commitment).

### The dependency/dependent asymmetry (the crux for lazy)

- **Dependencies point backward** to already-existing older runs and are stored in the dependent's
  own row. Adds/reservations/fulfills only need a node's *immediate dependencies* (plus bounded
  `workflow_next` chain walks) — a **local neighborhood**. Trivially lazy.
- **Dependents are the inverse** and `_drop_run_for_task(and_dependents=True)` walks the transitive
  dependent closure. You cannot know a node's dependents without either scanning the whole graph
  or **persisting a reverse index.**

This is why the sidecar persists **both** edge directions (see Stage 1). The eager Stage-1 path
could in principle derive dependents on load, but persisting them is what makes Stage 2 a behavior
change rather than a format migration.

### Whole-graph operations in the manager today

Audited: the only genuinely whole-graph operations are `drop_all` (iterates
`self.nodes.keys()`, session_manager.py ~1373) and `_save_run_graph_cache` (removed in Stage 1).
The `for node in self.nodes` at line 527 is over a **worker's** local `self.nodes: list`, not the
manager graph. Everything else is point membership/lookup (`task_cid in self.nodes`,
`self.nodes[task_cid]`) or the dependent-closure walk inside `_drop_run_for_task`. This shape is
very favorable for lazy.

---

## Stage 0 — regression tests (do first)

### Existing coverage (inventory)

Only two test files touch the session manager:

- `tests/test_tasks_and_providers.py`: materializes various task types in a **single** session and
  retrieves results (`test_materialization_succeeds`, `test_backend_runner_*`).
  `test_materialization_succeeds` reloads only the **runset sqlite** via
  `api_lib.load_active_runset` — it does **not** reopen a `SessionManager` or exercise graph
  reconstruction.
- `tests/test_plan_in_session.py`: `make_task_from`/bind tests; one in-session workflow
  materialization (`test_repeatedly_add_workflow_materialization`, 3 steps → 7 runs); and the one
  cross-session test, `test_run_graph_cache_persists_across_sessions`.

### Gaps (what is NOT covered)

The single reopen test exercises only the **warm** path with a **trivial single task**, and it
asserts on the implementation detail `_run_graph_cache.pickle` (which Stage 1 removes). Untested:

- **Cold rebuild correctness** — nothing deletes the cache and reopens to drive `_load_runset`'s
  expensive rebuild path and check the resulting graph.
- **Workflow across reopen** — `workflow_contains` / `workflow_next` reconstruction (the structure
  most likely to break in the edge-direction / commitment handling).
- **Multi-node dependency graph across reopen** — cross-task `TaskResultHandle` deps and the
  dependency/dependent edges they create.
- **Drop semantics across reopen** — dropping a node with transitive dependents.
- **In-progress / reservation present at shutdown** — the case the old cache discarded entirely.
- **Self-heal** — corrupt/partial cache → rebuild.
- No direct unit coverage of `ActiveRunManager` edge invariants (`_check_completed_run_deps`,
  bidirectional wiring, `_drop_run_for_task`); only indirect via `materialize`.

### Conclusion: add Stage 0

Coverage is too thin to refactor `_load_runset` and the graph representation safely. Add
behavior-level regression tests **first**. Write them **implementation-agnostic** — assert
observable behavior (results retrievable after reopen, runset membership, drop semantics), **not**
the cache filename/format — so the same tests guard both Stage 1 and Stage 2.

### Stage 0 test list

Build on the function-scoped `isolated_test_config` fixture and the `plan_fixtures` /
`plan_fixtures_stringized` modules (parametrize `use_stringized` as the existing tests do).

1. **Cold rebuild**: materialize a graph; close; **delete the cache file(s)**; reopen; assert all
   result handles still retrieve correctly and the runset is intact. (Forces the expensive path
   and checks it reconstructs a correct graph.)
2. **Multi-node deps across reopen**: task B depends on task A's `result_handle`; reopen; retrieve
   B; assert correct. Then assert dropping A also drops B (dependent closure) — both within a
   session and after reopen.
3. **Workflow across reopen**: materialize `repeatedly_add` (the 7-run workflow); reopen;
   instantiate the workflow result handle; assert final value. Exercises
   `workflow_contains`/`workflow_next` rebuild.
4. **Drop semantics**: drop a task with dependents; assert dependents gone and graph consistent;
   reopen; assert still consistent.
5. **In-progress at shutdown** (best-effort): leave a reservation/commitment active (or simulate a
   non-clean close); reopen; assert the completed-run subgraph still loads.
6. **Generalize the existing reopen test**: drop the hard-coded `_run_graph_cache.pickle`
   assertion; assert behavior ("reopen sees prior results") instead. Keep a separate, explicitly
   format-aware test for cache presence if desired, isolated so the format swap touches one place.

These are valuable on their own (they pin current correct behavior) and become the safety net for
Stages 1–2.

### Stage 0 — status (implemented 2026-06-15)

Tests live in `tests/test_runset_reload_regression.py`: cold-rebuild and warm-reload of a
multi-node dependency graph, workflow survives warm + cold reopen, drop-with-dependents within a
session, and drop-with-dependents after reopen. Result: **10 passed, 2 xfailed.**

**Bug found by these tests (pre-existing, in the current warm pickle cache).**
`_save_run_graph_cache` (session_manager.py ~1470-1483) builds shallow `RunNode` *copies* for the
cache, but their `dependencies`/`dependents` dicts still reference the **original** uncopied nodes.
After save→load, `self.nodes[cid]` is a copy while its edges point to a *different* clone, so
`_drop_run_for_task`'s `assert self.nodes[dependent.task_cid] is dependent` (line ~1289) fails when
dropping with `and_dependents` after a warm reopen. The within-session drop and the cold-rebuild
path are unaffected (they wire edges canonically via `_add_completed_run`). Captured as
`test_drop_after_reload_removes_dependent_closure` with `xfail(strict=True)`; Stage 1's sidecar
reconstructs edges from CIDs against the canonical node map and should make it pass — **remove the
xfail marker when it does** (strict xfail will then fail the suite, prompting removal).

### Environment / how to run (this worktree)

Incidental fixes needed to run the suite here (worktree not named `fxtr`):
- `tests/fxtr_testdeps/pyproject.toml`: `fxtr` source path was `../../../fxtr` (resolved to the
  sibling `fxtr` worktree); changed to `../../` (the current worktree root, name-independent).
- root `pyproject.toml`: the example/demo packages in the `test` dependency group are temporarily
  **commented out** — they reference external sibling checkouts (`behaviors`, `picoview`,
  `luce_lib`) absent here, which broke `uv` workspace resolution.
- root `pyproject.toml`: added missing runtime dep `tqdm` (imported by `session_manager.py`'s
  threaded loader but never declared).
- `.python-version` pinned to `3.13` (3.14 breaks fxtr's abstract-call-target check via
  `__annotations_cache__`; 3.12 breaks the existing tests' 2-arg `typing.Generator`).

Run: `uv run --group test pytest tests/test_runset_reload_regression.py -q`

---

## Stage 1 — durable incremental sidecar (both edge directions)

Replace the monolithic pickle with a **per-node sidecar keyed by `task_cid`**, updated
transactionally on every node mutation, storing forward **and** reverse edges. Still eager-loads
at startup (O(N) cheap row reads + O(N) object construction — the architectural floor for eager),
but is crash-durable and survives in-progress work.

### Storage (co-located in `active.sqlite` — revised after design review)

The cache records live **inside the runset's own SqliteDict** (`runsets/active.sqlite`), under keys
prefixed with `api_lib.RUN_GRAPH_CACHE_KEY_PREFIX` (`"_rg/"`), which is disjoint from base32 CIDs.
This was chosen over a separate sidecar file specifically so that **each mutation writes the runset
row and the affected cache rows in a single SqliteDict transaction** (`_commit_changes`) — making
membership and dependency edges atomic (see "Crash consistency" below). Confirmed via the
sqlitedict source that this is the only way to get cross-record atomicity: each `SqliteDict`
instance owns its own connection, so a *separate table* (or file) could not share a transaction;
disjoint keys in one table written through one handle can.

Runset rows are byte-identical to before; `api_lib._load_runset_from_sqlitedict` skips the `_rg/`
keys. `active.sqlite` stays **authoritative** for membership; cache rows are derived and fully
rebuildable from the entity store.

WAL was considered and **not** adopted: the existing full-overwrite path
(`api_lib.save_active_runset` → unlink + rewrite, used by the CLI / backend runner) conflicts with
WAL's sidecar files, and mixing journal modes across the many api_lib openers adds churn. Per-op
short transactions on a single process are fine.

Key: `task_cid` (base32, same encoding as runset).
Value (small pickled record) — everything needed to rebuild a `RunNode` for a **completed run**
with zero entity-store reads:

```
{
    "run": <thin core.Run object>,           # pickles via its IPLD form (core.py _unpickle_entity)
    "dependency_cids": [task_cid, ...],       # immediate deps that are completed RunNodes
    "dependent_cids":  [task_cid, ...],       # reverse index — REQUIRED for Stage 2's lazy drops
    "workflow_contains": [cid, ...] | None,
    "workflow_next": cid | None,
}
```

Notes:
- We persist the thin `Run` itself (small) so load needs no entity-store access;
  `node.run_or_reservation` must be a real `core.Run` (consumers read `core.entity_cid(run)` and
  `run.run_data`, e.g. `TaskHandleWorker._build_impl`).
- **Only completed-run nodes are persisted.** Reservations and workflow-commitment nodes are
  transient/in-memory and never written — this is what lets the cache stay valid during
  in-progress work (the old cache gave up entirely).
- `workers` is never persisted (rebuilt empty).
- `dependency_cids`/`dependent_cids` are derived at write time from the node's current
  `dependencies`/`dependents`, filtered to completed-run `RunNode`s (drop transient
  `WorkflowCommitmentNode`s).

### Accessor layer (do this in Stage 1 — biggest Stage-2 de-risker)

Route **all** graph access through methods instead of touching `self.nodes` directly in the ~18
sites:
- `self._get_node(task_cid) -> RunNode | None`
- `self._contains(task_cid) -> bool`
- `self._iter_dependents(node) -> Iterable[RunNode]`  (Stage 1: in-memory `node.dependents`;
  Stage 2: authoritative via persisted `dependent_cids`, lazily loaded)
- `self._set_node(node)` / `self._del_node(task_cid)`

In Stage 1 these are thin wrappers over the resident dict + sidecar writes. In Stage 2 we change
**only the accessors**, not logic scattered across the file.

### Maintenance hook points (keep sidecar in sync with the graph)

All node mutations live in `ActiveRunManager`. Exactly these change persisted state; each updates
**both endpoints atomically** (single sqlite transaction):

| Site | Line (approx) | Cache action |
|------|---------------|--------------|
| `_add_completed_run` | 991/1035 | **put** new node's row; append its cid to each dependency's `dependent_cids` |
| `fulfill_reservation` | 1076 | **put** row (reservation → completed run); update deps' reverse edges |
| `fulfill_workflow_commitment` | 1181 | **put** updated rows for each *dependent* whose `dependencies` got rebound (commitment → step node + next), updating both directions |
| `_drop_run_for_task` | 1259/1335 | **delete** node's row; remove its cid from each dependency's `dependent_cids` |
| `create_reservation` | 1054 | none (reservation not cached) |
| `cancel_reservation` | 1129 | none (drops an uncached reservation node) |

Re-writing rows on `fulfill_workflow_commitment` is what guarantees a node's persisted deps
converge to final RunNode deps (commitments are never persisted). This is the trickiest hook —
needs a targeted test.

### Load (warm path)

At the top of `_load_runset`, before the expensive rebuild:
1. Load runset (`active.sqlite`) → authoritative `task_cid -> run_cid`.
2. Open sidecar, **validate**: key set == runset keys, and each record's `run` cid ==
   `runset.runs[task_cid]`. Mismatch/corrupt/partial → discard, fall through to full rebuild
   (which repopulates). Self-healing. (CIDs are immutable, so a matching run cid ⇒ edges correct
   by construction; no need to validate edges.)
3. Pass 1: create every `RunNode` (run set; `dependents={}`, `workers={}`; `workflow_*` from row).
4. Pass 2: wire edges from `dependency_cids` (and assert/repair against `dependent_cids`).
5. No topo sort, no entity loads, no plan-dep walk.

### Build-on-first-load

`_add_completed_run` is already called for every node during the cold rebuild, so putting the
sidecar write inside it means the first cold load (no sidecar) **populates** the sidecar as it
rebuilds. Subsequent loads hit the warm path. The warm path sets nodes directly and must **not**
re-trigger writes.

### Bonus fix: stop redundant runset writes during cold load

During rebuild, `_add_completed_run` calls `insert_into_active_runset` (line 1038) for every node,
re-inserting rows already in `active.sqlite`, each with its own `SqliteDict` open/commit — N
redundant commits per cold load. Add `_add_completed_run(task, run, *, update_runset: bool = True)`
and have `_load_runset` pass `update_runset=False` (still write the sidecar during the rebuild).

### Stage 1 sqlite handle strategy

Single long-lived handle on the manager with batched commits during the initial populate (4M
writes); autocommit for steady-state single mutations. The rebuild's entity loads are threaded,
but `_add_completed_run` runs single-threaded after the topo sort, so writes are not concurrent.

### Stage 1 implementation sketch

1. `api_lib` (or small new module) helpers mirroring the runset ones: `get_run_graph_cache_path`,
   `load_run_graph_cache`, `put_run_graph_cache_entry`, `delete_run_graph_cache_entry`,
   `clear_run_graph_cache`, plus a transactional batch helper.
2. `ActiveRunManager`: add the accessor layer; `_write_cache_node(node)` /
   `_delete_cache_node(task_cid)` deriving rows + updating reverse edges atomically; call them at
   the four hook points; rewrite top of `_load_runset` (validate → two-pass rebuild); thread
   `update_runset=False`.
3. Remove `_save_run_graph_cache` + its shutdown call + the `.pickle` read block; one-time delete
   of any stale `_run_graph_cache.pickle`.
4. Tests (below).

### Stage 1 leaves these for Stage 2

- Startup is still eager O(N).
- `drop_all` still iterates the resident dict.
- Membership still uses an in-memory full map.

### Stage 1 — status (implemented 2026-06-15, co-located design)

Implemented in `fxtr/api/session_manager.py` (`ActiveRunManager`) + `api_lib.py`:
- Cache records **co-located in `active.sqlite`** under `_rg/`-prefixed keys (not a separate file).
  `api_lib.RUN_GRAPH_CACHE_KEY_PREFIX` is shared; `_load_runset_from_sqlitedict` skips those keys.
- **Legacy pickle migration**: `_migrate_legacy_pickle_cache(runset)` reads a pre-existing
  `<state_store>/_run_graph_cache.pickle` (the old monolithic format from the b9c8 branch), and if
  it is consistent with the runset, derives per-node records from it and writes them as `_rg/` rows
  — so an upgrading deployment **skips a cold rebuild** instead of throwing the pickle away. Only
  task CIDs are read from the pickled nodes, so the old shallow-copy edge bug is irrelevant; the
  pickle is deleted afterward and anything stale falls through to the normal cold rebuild.
- Record per completed-run node: `{run (thin core.Run), dependency_cids, dependent_cids,
  workflow_contains, workflow_next}` — both edge directions persisted (Stage 2 ready).
- **`_commit_changes(runset_inserts=, runset_deletes=, cache_puts=, cache_deletes=)`** is the one
  write path: it applies the runset row(s) AND cache row(s) in a single SqliteDict transaction, so
  membership and edges are atomic. Other helpers: `_node_to_cache_record`,
  `_rebuild_run_graph_cache` (bulk repopulate after cold load), `_load_from_run_graph_cache`,
  `_clear_run_graph_cache` (delete `_rg/` keys), `_runset_key`/`_cache_key`.
- Load path: `_load_runset` tries `_load_from_run_graph_cache` first (reads `_rg/` rows; validates
  cache key set == runset keys and each record's run CID; two-pass rebuild: create nodes, wire
  edges from `dependency_cids`). On miss/mismatch/corruption it cold-rebuilds, then
  `_rebuild_run_graph_cache` repopulates (removing stale `_rg/` rows) — self-healing and
  build-on-first-load.
- Maintenance hooks all go through `_commit_changes`: `_add_completed_run` (runset insert + put new
  node + deps), `fulfill_reservation` (same), `fulfill_workflow_commitment` rebind branch (cache
  only: step node + affected dependents), `_drop_run_for_task` (runset delete + cache delete +
  re-put surviving deps).
- `_add_completed_run` gained a single `persist` flag; the cold-load loop passes `persist=False`
  (fixes the redundant per-node runset writes during load; one bulk populate instead).
- `drop_all` suppresses per-node persistence (`_suppress_persistence`) and clears the whole store
  via `api_lib.save_active_runset(empty)` (drops runset + cache rows), avoiding one write per node.
- Removed `_save_run_graph_cache` and its shutdown call (cache is maintained incrementally).

**Fixes the Stage 0 bug.** `test_drop_after_reload_removes_dependent_closure` passes (xfail removed).

Tests: `tests/test_runset_reload_regression.py` **18 passed** (incl. self-heal-on-corruption,
reverse-index-consistency, and legacy-pickle-migration, all on the co-located `_rg/` rows).
`tests/test_plan_in_session.py` passes (cache-presence assertion now checks `_rg/` rows in
`active.sqlite`). Run:
`uv run --group test pytest tests/test_runset_reload_regression.py tests/test_plan_in_session.py -q`

Note: `tests/test_tasks_and_providers.py` is not runnable in this worktree — it loads the
`demo_09_15_fake_user` collection, one of the demo packages commented out of the `test` group
because their external sibling deps are absent here. Unrelated to this change.

### Migration script (`scripts/migrate_runset_cache.py`)

A standalone PEP 723 script (runnable via `uv run scripts/migrate_runset_cache.py <fxtr.toml>`,
pinning `fxtr @ git+...@fxtr1_durable_cache_migration`) to migrate an existing deployment's runset
to the co-located format. It backs up the `<data.active>/state` dir to a timestamped sibling, then
constructs an `ActiveRunManager` (the minimal trigger for `_load_runset`, no async / no python
provider), which migrates the runset format + populates the `_rg/` cache rows. tqdm progress is
shown (the bulk-write loops in `_rebuild_run_graph_cache` and `_migrate_legacy_pickle_cache` were
given tqdm bars for this). **Rollback requires the backup**: the new `active.sqlite` has `_rg/`
rows that pre-change code cannot read. Smoke-tested locally end-to-end.

### Crash consistency (resolved by co-location)

Because every mutation commits the runset row and the cache rows in **one** SqliteDict transaction,
a crash can never leave a node "in the runset" without its dependency edges, or vice versa — the
dependency invariant is never torn. The only remaining cross-store event is an external full
overwrite (`api_lib.save_active_runset`, CLI / backend runner), which unlinks + rewrites
`active.sqlite` and so drops all cache rows; that is correct (the whole runset changed) and the
cache is rebuilt on the next load. **This removes the per-node self-heal that the earlier two-file
design would have needed in Stage 2.**

### Known follow-ups / not yet optimized (Stage 1)

- `drop_from_container` and cascading drops still do one SqliteDict open (one transaction) per
  affected completed node. Fine for small/medium drops; revisit if large subgraph drops are hot.
- Each incremental mutation opens/commits/closes its own SqliteDict on `active.sqlite` (mirrors the
  prior runset helpers). Acceptable for steady-state single mutations; the only bulk path
  (cold-load populate) uses a single handle + one commit.
- No WAL (see Storage note); concurrent reader-during-writer could hit `SQLITE_BUSY` as before.

---

## Stage 2 — lazy (load-on-demand) graph, sub-linear restore

Flip the accessor layer from "resident dict" to "resident-or-load-from-sidecar". No on-disk format
change — Stage 1's records are exactly what a lazy loader fetches.

### Commit 1 — accessor refactor (done 2026-06-15, no behavior change)

Introduced the node-graph accessor layer on `ActiveRunManager` and routed every per-node access
through it: `_contains(cid)`, `_get_node(cid) -> RunNode | None`, `_set_node(node)`,
`_remove_node(cid)`, `_iter_dependents(node)`, `_resident_node_cids()`. All ~18 direct
`self.nodes[...]` / `in self.nodes` operational sites now go through these; the only remaining
direct `self.nodes` uses are the accessor bodies, the declaration, and the eager full-graph methods
(`_rebuild_run_graph_cache`, `_load_from_run_graph_cache`). In this commit the accessors are thin
wrappers over the resident dict — an exact no-op. All 28 tests stay green. This isolates the
behavior change in commit 2 to the accessor internals.

### Commit 2a — surgical-write groundwork (done 2026-06-15, behavior-preserving)

Implemented the Axis-1 piece (surgical reverse-index) ahead of the lazy flip, since it's
behavior-equivalent in eager mode (all 28 tests stay green):
- `_commit_changes` gained `dependent_additions` / `dependent_removals` (read-modify-write a
  parent's `dependent_cids` within the transaction) and now **preserves** a node's existing
  `dependent_cids` on `cache_puts` upsert instead of re-deriving from in-memory `dependents`.
- All four hooks switched to surgical ops (add/fulfill → `dependent_additions`; drop →
  `dependent_removals`; workflow rebind → rewrite dependents' own rows + add to step node).
- `RunNode.edges_loaded` flag added (always True in eager paths; False for future lazy stubs).
- `_open_store(autocommit=)` helper sets `PRAGMA busy_timeout` on every store open (SQLITE_BUSY
  handling); all store opens routed through it.

**Scope finding for 2b (external accessors):** the lazy flip must also route the *external*
readers of `manager.nodes` through the accessors — they would `KeyError` in lazy mode and commit 1
only covered the manager's own methods:
- `TaskHandleWorker._build_impl` (`self.manager.nodes[task_ref]`, two sites);
- `SessionManager` methods (`self._active_run_manager.nodes[...]`, ~4 sites).
These need `_get_node` / `_contains` (and `_ensure_edges` where they traverse edges). Widens 2b's
surface; noted so it isn't missed.

### Commit 2b — lazy flip (done 2026-06-15)

Implemented:
- `_fully_loaded` flag on the manager. `_load_runset` restructured around a cheap **count check**
  (`_store_counts` via GLOB `COUNT(*)`): runset-rows vs `_rg/` cache-rows. Equal → **lazy mode**
  (build nothing). Not equal (incomplete cache: external overwrite, old-format runset, un-migratable
  pickle) → one-time **eager rebuild** (`_eager_build_all`, factored out) which repopulates the
  cache so the next restart goes lazy.
- Lazy accessors behind `_fully_loaded`: `_get_node` → `_lazy_load_node` builds a **stub** from the
  cache row (edges empty, `edges_loaded=False`); raises a clear error if a runset row has no cache
  row (corruption — count check makes this impossible in practice). `_contains` → indexed
  `_runset_row_exists`. `_iter_dependents` → `_ensure_edges` then read.
- `_ensure_edges` wires a node's 1-hop neighborhood (both directions, merged) from its cache row;
  no-op when `edges_loaded` / `_fully_loaded` / `_suppress_persistence` (so `drop_all` doesn't
  cascade-load). Called at the top of `_drop_run_for_task`.
- Routed the **external** accessors through `_get_node`/`_contains`: `TaskHandleWorker` (2 sites)
  and `SessionManager` (`_worker_for_waiting_on_cached_reservation`, `_materialize_one_soon`,
  workflow-commitment worker).
- Removed the now-unused eager `_load_from_run_graph_cache`.

Tests: `tests/test_runset_reload_regression.py` **22 passed** (added lazy property tests: warm
reload builds zero nodes until access and loads only touched nodes; drop loads only the closure;
incomplete cache → eager rebuild + repopulate; lazy-miss-on-corrupt-cache raises).
`tests/test_plan_in_session.py` passes. Full: `32 passed`. The migration script was updated to
report `_store_counts` (in lazy mode `len(nodes)` is 0 even on success) and re-smoke-tested.

Perf follow-up (not blocking): lazy reads open a fresh `SqliteDict` per `_get_node`/`_ensure_edges`
(per-op `busy_timeout` connection). Fine for the small working sets lazy targets; a cached
read handle could be added if a hot path loads many nodes.

### Commit 2 — lazy flip: design scoping (2026-06-15)

Working the lazy flip through in detail surfaced that it is **not** just "flip the accessor
internals." The blocker is the in-memory edge representation: `RunNode.dependencies` / `dependents`
are `id()`-keyed dicts of **object references**, and once we stop loading the whole graph those
dicts become **partial** for any lazily-loaded node. Two sub-problems fall out, on two independent
axes.

#### Axis 1 — persisting edge *writes* (shared by every design): surgical reverse-index

Today a mutation re-derives a node's whole cache record from its in-memory state, e.g. adding
`T` (deps `D1..Dn`) does `_commit_changes(cache_puts=[T, D1..Dn])`, and `_node_to_cache_record(Di)`
rebuilds `Di`'s `dependent_cids` from `Di.dependents`. If `Di` was lazily loaded, `Di.dependents`
is **incomplete**, so rewriting `Di`'s row **silently drops** `Di`'s other dependents → corruption.

Fix (needed regardless of how reads work): maintain the persisted reverse index **surgically**,
never by re-derivation. Extend `_commit_changes` with:

- `dependent_additions: list[(parent_cid, child_cid)]` — read `parent`'s row, append `child` to its
  `dependent_cids`, write back (within the same transaction).
- `dependent_removals: list[(parent_cid, child_cid)]` — same, removing `child`.

And change `cache_puts` (upsert of a node's *own* row: run, `dependency_cids`, workflow info) to
**preserve the existing row's `dependent_cids`** (read-modify-write) rather than overwrite from the
possibly-incomplete in-memory `dependents`. A brand-new node has no existing row → `dependent_cids
= []`, which is correct (nothing depends on it yet).

Per-hook surgical calls:
- `_add_completed_run(T, deps)`: `runset_inserts=[(T, run)]`, `cache_puts=[T]`,
  `dependent_additions=[(Di, T) for Di in deps]`.
- `fulfill_reservation(node, deps)`: `runset_inserts=[(node, run)]`, `cache_puts=[node]`,
  `dependent_additions=[(Di, node) for Di in deps]`.
- `fulfill_workflow_commitment` rebind: each rebound dependent's `dependency_cids` changed
  (commitment → step node), so `cache_puts=[*rebound_dependents]`; the step node gains them as
  dependents, so `dependent_additions=[(step_node, dep) for dep in rebound_dependents]`.
- `_drop_run_for_task(X)`: `runset_deletes=[X]`, `cache_deletes=[X]`,
  `dependent_removals=[(Di, X) for Di in X's dependency_cids]` (X's dependents are dropped by the
  cascade). X's `dependency_cids` are read from X's cache row (immutable across restarts — see note).

Note: a completed run's `dependency_cids` are immutable across restarts. The only mutation is the
`fulfill_workflow_commitment` rebind, which happens **in-session** before the node is persisted with
its final deps; after a restart there are no live commitments, so persisted deps are stable.

#### Axis 2 — serving edge *reads*: two designs

When an operation needs a node's neighbors (drop cascade over dependents; drop reverse-cleanup over
dependencies; the reservation dependency-set check), where do they come from?

**Design A — disk-authoritative reads.** In-memory edge dicts are treated as partial/ignored for
lazily-loaded nodes; every edge read goes to the cache row.
- `_iter_dependents(X)` = (persisted `dependent_cids` → `_get_node` each) ∪ (in-memory `X.dependents`,
  to catch in-session reservation dependents that aren't persisted). Explicit union.
- Drop reverse-cleanup reads `X`'s persisted `dependency_cids`.
- Pros: loads the least. Cons: dual-source reads everywhere; explicit union logic; many
  "reservation vs completed / resident vs not" branches; highest subtlety.

**Design B — hybrid closure / `_ensure_edges` (recommended).** Restore the invariant the existing
code already assumes — *a node you're about to traverse has fully-populated in-memory edges* — by
loading its 1-hop neighborhood on demand.
- `_get_node(X)`: resident, else load a **stub** (run + workflow info; empty edge dicts;
  `edges_loaded=False`). In-session-created nodes (reservations, new runs) are born edge-complete.
- `_ensure_edges(X)`: if not `edges_loaded`: for each persisted `dependency_cid` and
  `dependent_cid`, `_get_node` the neighbor (stub is fine) and wire **both** directions, merging
  into the existing dicts; mark `edges_loaded=True`. This naturally **merges** persisted dependents
  with any in-session reservation dependents already in `X.dependents` — so the union problem from
  Design A disappears.
- All edge traversal goes through accessors that call `_ensure_edges` first (`_iter_dependents`, a
  new `_iter_dependencies`). Reads that don't traverse edges (attaching a result handle) never call
  it, so they stay minimal.
- Persistence stays **surgical** (Axis 1), so an `add` does *not* need to `_ensure_edges` its deps —
  it just surgically appends. `_ensure_edges` is needed only before edge *traversal* (drops).
- Pros: **unifies eager and lazy** (eager is just "every node resident + edges loaded", so
  `_ensure_edges` is a no-op and the existing traversal/mutation code runs unchanged); reuses the
  well-tested drop logic by inserting `_ensure_edges` calls; union handled for free. Cons: a node
  carries an `edges_loaded` flag; drops load the closure + 1-hop deps (same working set as A).

Recommendation: **Design B.** It localizes the new logic to loading (`_get_node` / `_ensure_edges`)
and keeps the consistency-critical mutation/traversal code essentially as-is, which is the lower-risk
path for a structure that must never corrupt. Stubs are loaded lazily, so "totally irrelevant"
nodes are never touched — matching the "little closure" intuition.

#### Shared infrastructure (both designs)

- **Startup**: avoid per-node entity reconstruction by a cheap **row-count check** — compare the
  count of runset rows to co-located cache rows (`_rg/` keys). If equal, the cache is complete →
  go lazy, build nothing. If not (e.g. after an external `save_active_runset` dropped the cache, or
  a first-ever load), do the existing **one-time eager rebuild** (`_load_runset` cold path +
  `_rebuild_run_graph_cache`) to fully populate the cache, then proceed; subsequent restarts find a
  complete cache and go lazy. This means a lazy `_get_node` can assume the cache row exists for any
  runset member (no entity-store fallback / no hard workflow-path reconstruction).
- **`busy_timeout`**: set `PRAGMA busy_timeout` on the store connections so a read during a write
  retries instead of erroring (the easy SQLITE_BUSY handling we agreed on; no WAL).
- **`drop_all`**: already clears the store wholesale (`save_active_runset(empty)`); in lazy mode it
  iterates only resident nodes for in-memory teardown (non-resident nodes have no in-memory state),
  which is correct.

#### Risks / test matrix (when implemented)

- reverse-index integrity after long add/fulfill/drop sequences (extend the existing
  `test_run_graph_cache_reverse_index_is_consistent`);
- drop a node whose dependents were never made resident (closure found via persisted
  `dependent_cids`);
- in-session reservation dependent + persisted dependent on the same node, then drop (union);
- workflow commitment rebind then restart then drop;
- cache-incompleteness (simulate external overwrite) → one-time eager rebuild → then lazy;
- the headline assertions: warm load constructs **zero** nodes until first access; a post-restart
  `materialize` of one new task loads only its dependency chain (bounded, independent of runset
  size).

### Startup

Load **nothing** (or only what's needed for the immediate operation). Membership is an **indexed
sqlite lookup** against the runset / sidecar (O(log N)) — the "constant/logarithmic restore"
target. The manager keeps a resident map (identity cache) of nodes loaded this session.

### Lazy resolution

- `_get_node(cid)`: return the resident node, else read the sidecar row, construct the `RunNode`,
  register it resident, and return it. (Level 1: never evicted ⇒ stable `id()` ⇒ existing
  `id()`-keyed edges and direct references keep working.)
- `_contains(cid)`: resident map OR indexed sidecar/runset key existence — no full scan.
- Wiring edges on load: when a node is materialized, resolve its `dependency_cids` to nodes (load
  them if needed for the current operation) and link both directions among resident nodes.

### The critical correctness rule

A resident node's in-memory `dependents` dict is **not authoritative** — a dependent not yet
touched won't be in it. Any operation that walks dependents (only drops do) must consult the
**persisted `dependent_cids`** and lazily load them, never just `.dependents`. Reads/adds never
walk dependents, so they're unaffected. This is the single place a naive port would silently
corrupt the graph — prominent comment + targeted test.

### How each invariant holds under lazy

| Invariant | Lazy enforcement |
|---|---|
| One run/reservation per task | `_contains` via resident ∪ indexed sidecar key; no scan. |
| Runset consistency (deps present) | `dependency_cids` are in the sidecar by construction. |
| Bidirectional edges | Both endpoints written atomically (Stage 1); on load, link via persisted edges. |
| Provenance matching | Validated when the run was first added (Stage 1); CIDs immutable ⇒ still valid. Re-validate lazily on access only if we want belt-and-suspenders. |
| Worker tracking | A node with a worker is necessarily resident; drop walks the persisted dependent closure, loading each and marking its (resident) workers stale; un-resident dependents have no workers. |
| Workflow commitments | In-memory/transient; bounded `workflow_next` chain walks load only the path. |

### Operations and their cost under lazy

- `instantiate` / `materialize` after restore: loads only the touched plan's dependency chain —
  sub-linear in runset size.
- `create_reservation` / `_add_completed_run` / `fulfill_reservation`: load immediate deps +
  bounded workflow path. Local.
- `_drop_run_for_task(and_dependents=True)`: walk the **persisted** dependent closure, loading each
  node once. Cost ∝ closure being dropped (inherent), not the whole graph.
- `drop_all`: becomes "clear sidecar + resident map" — no node loads. (Must replace the current
  resident-dict iteration.)

### What Stage 2 must change (mostly mechanical)

- Accessor internals: resident-only → resident-or-lazy-load; `_contains` → indexed lookup;
  `_iter_dependents` → authoritative via persisted `dependent_cids`.
- `drop_all`: iterate sidecar keys / clear, instead of the resident dict.
- Drop closure: walk persisted `dependent_cids`, not in-memory `.dependents`.
- Audit for any remaining assumption that `self.nodes` is complete.

### Explicitly NOT in scope

- Eviction / bounded resident memory ("Level 2"): would require re-keying edges by `task_cid`
  instead of `id()` and resolving neighbors through the manager. Not needed — the pain is cold
  startup, not steady-state memory.

---

## Open questions

- **Validation strictness**: keys == runset keys AND run cids match — believed sufficient (edges
  derive from immutable, CID-keyed data). Confirm.
- **Partial write / crash mid-populate**: with batched/atomic writes the sidecar is a subset on
  crash; key-set validation rejects → full rebuild. Acceptable for v1; could later make populate
  resumable (treat a valid subset as "continue building the rest").
- **sqlitedict at 4M**: confirm scan + per-row pickle cost; confirm handle/commit strategy under
  the threaded loader.
- **Record size at 4M**: storing the thin `Run` + both edge lists per row — confirm total size is
  acceptable; if not, fall back to storing `run_data`/`task`/`provenance` cids + `run_info` and
  rebuilding the `Run`.

## Test plan

- **Round-trip**: graph with tasks, a workflow with steps, cross-deps; restart; assert
  `nodes`/`dependencies`/`dependents`/`workflow_contains`/`workflow_next` match.
- **Incremental**: add/drop, fulfill a reservation, fulfill a workflow commitment; after each,
  restart and assert correctness (cache stayed in sync). Special focus on
  `fulfill_workflow_commitment` rebinding both edge directions.
- **Self-heal**: corrupt/trim the sidecar → full rebuild + repopulate.
- **In-progress**: leave a reservation/commitment active at shutdown → completed-run subgraph
  still loads from cache on restart (old behavior lost the whole cache).
- **Reverse-index integrity**: assert `dependent_cids` is the exact inverse of `dependency_cids`
  across the sidecar after a sequence of mutations.
- **Stage 2 — laziness**: assert warm load does **zero** entity-store reads and constructs **zero**
  nodes until first access; a post-restore `materialize` loads only the touched dependency chain
  (bounded node count, independent of runset size); a drop loads only the dropped closure.
- **Stage 2 — dependent authority**: drop a node whose dependents were never made resident; assert
  the full closure is dropped via persisted `dependent_cids`.

---

## Correctness invariants (why lazy loading is safe)

This section states, precisely, what makes the implementation correct — especially once nodes are
loaded lazily and the in-memory graph is only a partial view.

### The three stores and what each is authoritative for

1. **Entity store** (`<data.active>/entities`, CID-addressed CBOR). Immutable content-addressed
   blobs (runs, tasks, provenance, run data). Read-only here; never mutated by the run manager.
2. **Runset rows** (`active.sqlite`, base32-CID keys → `run_cid`). **Authoritative for membership**:
   a task has a completed run in the active set iff it has a runset row. This is the canonical,
   format-stable store (other tools read it).
3. **Cache rows** (`active.sqlite`, `_rg/`-prefixed keys → `{run, dependency_cids, dependent_cids,
   workflow_contains, workflow_next}`). **Authoritative for the graph structure** of completed
   runs. Derived from the entity store; rebuildable; co-located with the runset so it can be
   written in the same transaction.

In-memory `ActiveRunManager.nodes` is a *materialized view*: complete when `_fully_loaded`, a
subset in lazy mode. It additionally holds **transient** state (reservations, workflow commitments,
workers) that is never persisted.

### Invariants

- **I1 — Atomic co-write.** Every persisted mutation goes through `_commit_changes`, which applies
  all runset-row and cache-row changes in a single SqliteDict transaction. ⇒ a completed run's
  membership and its cache row (run + edges) always appear/disappear together; a crash cannot
  separate them.
- **I2 — Cache completeness in lazy mode.** Whenever `_fully_loaded is False`, every runset row has
  a corresponding cache row. *Established* at startup by the count check (`runset rows == _rg/
  rows` ⇒ lazy; otherwise eager rebuild). *Preserved* by I1 (every later mutation keeps the two
  1:1). The only way to break the count equality — an external `save_active_runset` full overwrite
  that unlinks/rewrites the file and drops all cache rows — is caught at the next startup
  (`cache_count != runset_count`) and triggers a one-time eager rebuild, not lazy mode.
- **I3 — Cache edge correctness.** For a completed run X, `X.dependency_cids` is exactly X's
  dependency set among completed runs (fixed when X is added; immutable thereafter except the
  in-session workflow-commitment rebind, which rewrites X's row before any restart). `X.dependent_cids`
  is exactly the set of completed runs currently depending on X — maintained **surgically**
  (`dependent_additions`/`dependent_removals`), never re-derived from the (possibly partial)
  in-memory `dependents` dict.
- **I4 — Reverse index is the exact inverse.** For completed runs X, Y: `Y in X.dependent_cids`
  ⟺ `X in Y.dependency_cids`. Holds because each add/drop updates both endpoints in the same I1
  transaction.
- **I5 — In-memory faithfulness.** A resident completed-run node's `run_or_reservation` and
  `workflow_*` equal its cache row. Its edge dicts are either empty-pending (`edges_loaded=False`)
  or complete (`edges_loaded=True`); when complete they equal (cache edges) ∪ (in-session
  transient edges). `_ensure_edges` establishes completeness on demand by loading the 1-hop
  neighborhood and **merging** (never overwriting) into the dicts.
- **I6 — Transient state is memory-only.** Reservations and workflow commitments are never written
  to either store. After a restart they are gone, which is consistent because the runset only ever
  contains completed runs.
- **I7 — Identity map.** `_get_node(cid)` returns the *same* `RunNode` object for a given cid for
  the manager's lifetime (the resident `self.nodes` is the identity cache; lazily-loaded stubs are
  registered there). Required because edges are keyed by `id()` and some logic compares `id()`s
  (e.g. the dependency-set check in `fulfill_reservation`).

### Why lazy loading produces the same graph as eager loading

By I2 + I3 + I4, the cache rows are a **complete and internally-consistent serialization of the
completed-run graph**. Lazy reconstruction builds nodes and edges purely from the cache (membership
from the runset). Therefore any node `_get_node` materializes, and any edge set `_ensure_edges`
wires, is identical to what a full eager load would have produced. Every operation reaches the graph
only through the accessors (`_contains`, `_get_node`, `_ensure_edges`, `_iter_dependents`), so each
operation observes exactly the eager graph — just materialized on demand. Lazy mode is thus
behavior-equivalent to eager mode; it only changes *when* objects are built. (This is also why
Stage 2 needed no on-disk format change: the cache already had to be a faithful serialization.)

### The subtle points, made explicit

- **Surgical reverse-index is necessary, not an optimization.** In lazy mode a node's in-memory
  `dependents` is partial. Re-deriving its cache `dependent_cids` from that partial dict (the old
  approach) would silently drop unloaded dependents, violating I3/I4. Surgical add/remove touches
  only the single changed entry in the persisted row, so I3/I4 hold regardless of what is resident.
- **The reservation↔completed union is handled by merging.** A reservation R that depends on a
  completed node X is in-memory only (I6); X's cache row does not list R. `create_reservation`
  wires R into `X.dependents` in memory; `_ensure_edges(X)` *merges* the persisted dependents into
  `X.dependents` rather than replacing, so both R and the persisted dependents are present. When R
  is fulfilled it is added to X's cache `dependent_cids` surgically. So drops over `_iter_dependents(X)`
  see both transient and persisted dependents.
- **No entity-store access on the lazy path.** The cache row stores the thin `Run` object directly,
  so reconstructing a node/edge never reads the entity store. (Entities are dereferenced only when a
  consumer actually needs run *data* — e.g. retrieving a result — via the CID references on the Run.)
- **A lazy cache miss is corruption, by I2.** When lazy, a runset row with no cache row cannot occur
  under I1/I2; `_lazy_load_node` raises a clear error pointing at re-running the migration, rather
  than silently reconstructing (which would require the hard, sometimes-impossible per-node
  recovery the count check was designed to avoid).
- **`drop_all` is exempt from edge maintenance.** It sets `_suppress_persistence`, which makes
  `_ensure_edges` and `_commit_changes` no-ops, tears down only resident in-memory state, then
  clears the whole store wholesale (`save_active_runset(empty)`). Non-resident nodes have no
  in-memory state to tear down and their rows are removed by the wholesale clear, so the result is
  consistent without cascade-loading the graph.
