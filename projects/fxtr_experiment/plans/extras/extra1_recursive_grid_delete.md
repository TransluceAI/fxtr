# Recursive grid delete

**Status: not started.** A standalone follow-up (not logically part of
any phase — hence `plans/extras/`), prompted by a dashboard deadlock:
you can offer "Delete grid" on a top-level task grid, but the call
always fails once the task has run.

## Motivation

`delete_grid` is **rmdir-style** (`state_store._delete_grid`): it
refuses when the grid has child grids —

```
if children:  # SELECT COUNT(*) FROM grids WHERE parent_path = key_path
    raise StateDbError("grid … has N child grids; delete leaves first")
```

A task grid's **internals are child grids** (a checkpoint grid
`["t","steps"]` has `parent_path = ["t"]`), and internals are read-only
from the top (you can't get a write subtree grant addressing inside a
task's internals). So "delete the leaves first" is impossible and the
task grid can never be deleted — a genuine deadlock.

The fix (anticipated in `notes/grounding.md` § "Grid dependencies and
deletion"): a **recursive delete** that removes a grid and every grid
under it. The grounding-consequence machinery (the reverse
rule-reference index → `DeleteReport.broken_referrers`) already exists;
this generalizes the delete from one grid to a subtree.

## Design

### Surface

`delete_grid` gains a keyword-only **`recursive: bool = False`** flag
(the shape of `add_target`'s `adopt` / `revoke_grid_sourcing`'s
`force`):

- `recursive=False` — today's safe rmdir (refuse on child grids). The
  careful/programmatic default; unchanged behavior.
- `recursive=True` — delete the grid **and all descendant grids**.

**Proceed-and-notify** on external breakage (as today): deleting a
subtree flips the grounding of external dependents whose `grid_dep`
references vanish; those are returned in `DeleteReport.broken_referrers`
and surfaced in the action log. No refuse, no dry-run preview (the UI
already shows the consequences after the fact).

The **grant/authority surface is unchanged** — `delete_grid` still
takes the parent subtree grant + key; `recursive` only changes what the
session does with the resolved `grid_path`.

### Session mechanism (`state_store`)

One transaction, generalizing `_delete_grid` from a single grid to the
**deleted set D** = the target grid + all its descendants:

```
D = recursive ? descendants(key_path) : [key_path]      # parent_path recursion
if D is empty:                       raise "no grid at key_path"
if not recursive and has_children:   raise "… has N child grids; delete leaves first, or delete recursively"
if any running generation in D:      raise "… running generations"   # the one real guard, over D
seeds = collect_external_seeds(D)    # BEFORE the cascade (see below)
DELETE FROM grids WHERE key_path IN D        # cascade sweeps each grid's cells + task_execs (+ their records)
for g in D: delete_wanted_stamps_under(g); _mark_grid_all(g)
mark_grounding(seeds)
return external_referrers(D)         # broken_referrers
```

- **Descendant enumeration** via a recursive CTE over `parent_path`
  (robust; uses the `grids_by_parent` index), *not* JSON-text prefix
  matching (`["t"]` would falsely match `["t2"]`):

  ```sql
  WITH RECURSIVE d(key_path) AS (
    SELECT key_path FROM grids WHERE key_path = ?
    UNION ALL
    SELECT g.key_path FROM grids g JOIN d ON g.parent_path = d.key_path)
  SELECT key_path FROM d
  ```

  This naturally includes nested internals and tasks-inside-internals
  (e.g. the demo's `planned` task that creates a nested `polish` task).

- **One `DELETE` over the whole set** works because `grids` has no FK
  on `parent_path` (it's denormalized) — so there is no inter-grid
  ordering constraint; each deleted grid row independently cascades its
  cells/generations via the existing `ON DELETE CASCADE`. Per-cell
  internals need no special handling (dropping the internals grid drops
  all its coords regardless of owning cell).

- **Seeds enumerated before the cascade** — the unioned generalization
  of the current three seed families, self-filtered to exclude subjects
  inside D (marking a generation that's about to be deleted is wasted).
  This ordering is required because the marking reads rows the cascade
  destroys (the departing generations' claim/demand edges); it is
  inherent to the operation, not an artifact of cascade (see the
  2026-07-14 discussion — we keep cascade; it is load-bearing for
  generation turnover and gives structural integrity):

  - external referrers: `grid_refs WHERE referenced_path IN D AND referrer_path NOT IN D`;
  - external readers: the `subject_versions → observations → task_execs`
    join with `sv.grid_path IN D AND te.grid_path NOT IN D`;
  - the would-start fan of every generation in D (`task_execs WHERE grid_path IN D`).

  `broken_referrers` is the external-referrers set (sorted).

Keep the non-recursive path behavior-identical to today (the rmdir
refusal + the single-grid seeds), so nothing downstream of the current
`delete_grid` changes unless the caller opts into `recursive`.

### Wire + capability

- `user_api.UserCapabilityProto.delete_grid(subtree, key, *, recursive=False)`
  (proto + docstring: the recursive contract, the running-guard,
  proceed-and-notify).
- `user_service.UserCapabilityImpl.delete_grid` forwards `recursive` to
  the session; `Workflow.delete_grid` + the `SubtreeView.delete`
  wrapper gain the flag (default False keeps their current contract).

### Frontend

- `wire/proto.ts`: `delete_grid` gains the `{ recursive?: boolean }`
  kwarg slot; the `delete_grid` `ActionStep` (`panel_state.ts`,
  `actions.ts`) carries `recursive`.
- `grid_view.tsx` `GridActions`: when the grid has descendants
  (`node.children` non-empty — internals included), the delete confirm
  fires `recursive: true`. **No count** (a tree node's `children` are
  only its direct children, so any count risks undercounting nested
  internals — better no number than a misleading one). The wording
  names *what* recursive means instead: "delete this task and all of
  its internal subgrids?" for a task grid, "delete this grid and all of
  its descendants?" for a subtree. A leaf grid keeps the plain rmdir
  confirm. The resulting `broken_referrers` render in the action log.

## Decisions settled

- `recursive` **flag**, default `False` (safe rmdir preserved).
- **Proceed-and-notify** on external breakage; no dry-run preview.
- **Keep `ON DELETE CASCADE`** — the seeds-before-delete ordering is
  inherent and localized; cascade is load-bearing for turnover and is a
  structural integrity guarantee.

## Tests (`tests/`)

- Recursive delete of a task grid with internals succeeds (the
  deadlock case); the internals grids + all generations are gone.
- Nested internals (task-in-internals) fully removed.
- Running-guard: refuses if any generation anywhere in the subtree is
  running.
- Non-recursive delete still rmdir-refuses on child grids (unchanged).
- External dependents: a grid outside D referencing a grid in D is
  reported in `broken_referrers` and its grounding flips; a re-create
  at the same path re-grounds it (marking in the other direction).
- Grounding correctness: an external reader of a deleted subject is
  re-marked; the safety-pass/currency invariants hold.
- Wire smoke through the capability; the frontend `deriveCellActions` /
  `GridActions` confirm path (recursive flag threaded).

## Steps

- [x] `state_store`: recursive `_delete_grid` (descendant CTE, set-based
      running guard + unioned external seeds, `NOT IN D` filters,
      single-`DELETE` over the set); non-recursive path identical.
      Backend tests (`test_state_store`: subtree removal, deep,
      external-referrers-only + the running/rmdir guards). *(2026-07-15)*
- [x] `user_api` / `user_service`: the `recursive` keyword arg on the
      proto, impl, `Workflow.delete_grid`, and `SubtreeView.delete`; a
      wire recursive-delete test (`test_user_surface`). *(2026-07-15)*
- [x] Frontend: `proto.ts` kwarg, the `delete_grid` `ActionStep` +
      `allocateStep`, the `GridActions` recursive confirm (wording off
      `hasDescendants` + task-ness, no count). *(2026-07-15)*
- [ ] Verify: browser pass (delete a run task grid with internals; see
      it vanish). Backend (253) + frontend (44) suites green.
- [ ] Update this plan's status. (No `CODE_OVERVIEW.md` `delete_grid`
      line to touch.)
