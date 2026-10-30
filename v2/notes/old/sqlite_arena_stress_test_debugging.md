# SQLite Arena Stress Test Debugging

## Overview

Debugging flaky stress tests in `tests/test_sqlite_arena.py` for the `SQLiteNamespaceArena` class.

## Test Structure

The stress tests use a `StressTestFixture` that:
- Creates a memory arena with an n-node and its underlying d-node
- Creates a SQLite session with garbage worker **disabled** (`_should_start_garbage_worker=False`)
- Provides manual control over GC via `run_gc_until_done()`
- Tests various orderings of `copy_from_arena` (store) and `abandon` operations

Key timing controls:
- `_delay_pending_tasks`: Delays pending async tasks until context exits
- `_wait_for_all_pending_tasks()`: Waits for pending tasks to complete
- `_clean_up_garbage()`: Manual GC step

## Hypotheses

### Hypothesis 1: Race between abandon and copy_from_arena

**Description**: When we abandon an entity and then re-copy it, there may be a race where:
- The abandon's pending task removes the entity from the arena/namespace
- The copy_from_arena sees the entity as already in entity tables but not in namespace
- The TrackDeps logic doesn't correctly handle this case

**Evidence for/against**: TBD

### Hypothesis 2: Entity reservation not protecting against GC

**Description**: Entity reservations are meant to protect entities during copy operations, but may not be checked during garbage collection.

**Evidence for/against**: Looking at `_is_entity_orphaned_in_namespace`, it checks:
- is_required (namespace backrefs)
- is_pinned
- in_arena

It does NOT check entity_reservations. This could be a bug.

### Hypothesis 3: _db_tracked vs _tracked mismatch

**Description**: The arena maintains `_tracked` (Python set of FxtrIDs) and `_db_tracked` (set of internal IDs reflecting DB state). These must match after pending tasks complete. A mismatch could cause issues.

**Evidence for/against**: TBD

## Test Runs

### Run 1: Initial run without logging

**Failures (4 tests):**
1. `test_segment4_store_n_d_abandon_n_d__gc_after_pending` - KeyError: 2
2. `test_segment7_store_d_n_abandon_n_d__gc_after_pending` - KeyError: 2
3. `test_loop_store_abandon_n_only__with_async_gc_worker` - TBD
4. `test_loop_store_abandon_n_only__with_async_gc_worker_no_wait` - TBD

**Error pattern:** `KeyError: 2` in `_do_abandon` at line 2955:
```python
if (
    self._internal_id_to_fxtr[internal_id] not in self._tracked  # <-- KeyError here
    and self._precomp_value_count[internal_id] == 0
):
```

### Analysis of Bug 1: Premature precomp cleanup in transitive abandon

**Scenario (test_segment4):**
1. store n (n-node depends on d-node via UNWRAPS_TO)
2. store d
3. abandon n - schedules `_do_abandon(n)`
4. abandon d - schedules `_do_abandon(d)`
5. wait for pending tasks

**What happens:**
1. `_do_abandon(n)` runs first:
   - Removes n from arena_membership and _db_tracked
   - Checks if n can be removed from precomp: n not in _tracked ✓, value_count == 0 ✓
   - Removes n from precomp, decrements d's value_count
   - Since d's value_count becomes 0 AND d not in _tracked, d gets added to `to_remove`
   - d gets removed from `_internal_id_to_fxtr`, `_precomp_value_count`, etc.

2. `_do_abandon(d)` runs:
   - Tries to access `self._internal_id_to_fxtr[d_internal_id]`
   - **KeyError!** d was already removed by n's transitive cleanup

**Root cause:** The precomp cleanup in `_do_abandon` is too aggressive - it removes entities transitively even if those entities have their own pending `_do_abandon` task that hasn't run yet.

**Proposed fix:** Add a check for `internal_id not in self._db_tracked` before removing from precomp. An entity should only be cleaned from precomp after its own `_do_abandon` has run (which removes it from `_db_tracked`).

### Analysis of Bug 2: GC doesn't check entity reservations

**Scenario (test_loop_store_abandon_n_only__with_async_gc_worker):**
- Multiple cycles of store n, abandon n, wait
- Async GC worker running in background

**Error:**
```
RuntimeError: Entity <FxtrID niuiqecs> not found in namespace after copy
```

**What happens:**

During `copy_from_arena`:
1. `_build_tmp_arena_copying_from_external_arena` starts
2. Entity is in entity tables (from previous cycle) but may have been removed from namespace by GC
3. `_DiscoverWork`: Entity found in DB, reservation added, queues `_TrackDepsWork`
4. `_TrackDepsWork`: Entity NOT in namespace, queues `_TrackWork` (entity still has reservation!)
5. Meanwhile, async GC runs `_is_entity_orphaned_in_namespace`
6. GC sees entity is not required, not pinned, not in arena... **but doesn't check reservations!**
7. GC removes entity from `namespace_entity_ownership`
8. `_TrackWork` runs but entity was just removed from namespace

**Root cause:** The `_is_entity_orphaned_in_namespace` method docstring says "not reserved by any session" but the actual implementation doesn't check `entity_reservations_table`.

Looking at the code:
```python
async def _is_entity_orphaned_in_namespace(...):
    """... and not reserved by any session."""
    is_required = exists().where(...)
    is_pinned = exists().where(...)
    in_arena = exists().where(...)
    result = await conn.execute(select(or_(is_required, is_pinned, in_arena)))
    return not result.scalar()
```

**Proposed fix:** Add `is_reserved` check to `_is_entity_orphaned_in_namespace`:
```python
is_reserved = exists().where(
    entity_reservations_table.c.entity_id == entity_id
)
result = await conn.execute(select(or_(is_required, is_pinned, in_arena, is_reserved)))
```

### Analysis of Bug 2b: GC removes child before parent backref created

**Error:**
```
RuntimeError: Internal invariant violation: child ID 3 is not in namespace 1
```

**Location:** `_add_entity_to_namespace_if_needed` at line ~1884

**What happens:**

During `_build_tmp_arena_copying_from_external_arena`:
1. Child entity Y is added to namespace
2. Reservation on Y is released (or Y is in temp arena)
3. GC runs and removes Y from namespace (sees Y as orphaned)
4. Parent entity X is being added to namespace
5. X has a dependency on Y
6. Code tries to create backref from X to Y
7. Check for Y in namespace fails - "child ID is not in namespace"

**Root cause:** Same as Bug 2 - GC doesn't check reservations. This error happens at a different code path but same underlying cause.

## Summary of Bugs

| Bug | Type | Error Message | Root Cause |
|-----|------|--------------|------------|
| 1 | Deterministic | KeyError in `_do_abandon` | Premature precomp cleanup removes entity before its own `_do_abandon` runs |
| 2a | Race (async GC) | "Entity not found in namespace after copy" | GC removes entity during copy - doesn't check reservations |
| 2b | Race (async GC) | "Internal invariant violation: child ID X is not in namespace" | GC removes child between adding it and creating parent's backref |

## Proposed Fixes

### Fix for Bug 1
In `_do_abandon`, add check for `internal_id not in self._db_tracked` before removing entity from precomp cache:
```python
if (
    self._internal_id_to_fxtr[internal_id] not in self._tracked
    and internal_id not in self._db_tracked  # <-- ADD THIS
    and self._precomp_value_count[internal_id] == 0
):
    to_remove.append(internal_id)
```

Also in the while loop for transitive cleanup:
```python
if new_count == 0:
    ref_fxtr = self._internal_id_to_fxtr[referenced_id]
    if ref_fxtr not in self._tracked and referenced_id not in self._db_tracked:  # <-- ADD THIS
        to_remove.append(referenced_id)
```

### Fix for Bug 2a/2b
In `_is_entity_orphaned_in_namespace`, add reservation check:
```python
is_reserved = exists().where(entity_reservations_table.c.entity_id == entity_id)
result = await conn.execute(select(or_(is_required, is_pinned, in_arena, is_reserved)))
```
