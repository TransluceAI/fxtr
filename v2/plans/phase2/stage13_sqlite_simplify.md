# Stage 13: SQLite Store Simplification

## Goal

Rewrite `SQLiteNamespaceArena` to be a lightweight, read-only view into the SQLite database, eliminating the overhead of DB arena creation/disposal, task scheduling queues, and write operations for every retrieve/load/abandon cycle.

## Background

The current `SQLiteNamespaceArena` is a `CopyTargetArena` that:
- Creates a DB arena row for every retrieve/lookup (write pool)
- Maintains a task scheduling queue for serialized async operations
- Writes to `arena_membership` on every track/abandon operation (write pool)
- Writes to `namespace_entity_ownership`/`namespace_entity_backref` when loading children (write pool)
- Uses weakref finalizers to schedule DB arena disposal
- Creates new p/u child entities in the entity table during load (write pool)

This causes significant overhead: many small write transactions dispatched to the thread pool, complex async sequencing, and DB arena lifecycle management.

### Key insight

GC already only removes namespace metadata — it never deletes from `entity`, `stored_entity_data`, or `entity_dep`. So entity data persists regardless of arenas. The DB arenas' only purpose is protecting namespace metadata from GC, but since loads can read directly from entity tables, we don't need that protection for user-facing arenas.

## Design

### New SQLiteNamespaceArena

- Implements `ReadableArena` (not `CopyTargetArena`)
- **No DB arena**: no `arena_membership` row, no internal arena ID, no finalizer
- **No write operations**: all operations are either pure Python or read-pool DB queries
- **No task queue**: no pending tasks, no event-chain scheduling
- Holds: session reference (for pool access), namespace reference, Python `_tracked` set, precomp data dicts
- `load()`: only supports d/n nodes; raises `NotImplementedError` for p/u/c/./! nodes with a message directing users to load via a MemoryStore (using `unsafe_lazily_attach_from_arena`)
- `abandon()`: removes from Python `_tracked` set and cleans up precomp caches. No DB writes.
- `copy_to_new_arena()`: creates a new Python-only arena with copied precomp data. No DB arena.
- `close()`: clears Python state. No DB disposal.
- `unwrap_provenance()`, `c_node_to_context_data()`, etc.: same as before (pure Python using precomp data), but instead of scheduling a DB write via `_track_from_precomputed`, just add to `_tracked` directly.

### How loads work

For d/n nodes, `load()`:
1. Read CBOR bytes from `stored_entity_data` (read pool)
2. Deserialize to `FxtrCbor`
3. Extract child FxtrIDs from the CBOR
4. Look up children's internal IDs in the `entity` table (read pool)
5. Load precomp data for children via `load_precomp_data_for_entities` (read pool)
6. Add children to `_tracked` and populate precomp dicts
7. Return the CBOR data

All reads, no writes. This works because all children already exist in entity tables — they were inserted when the parent entity was originally copied into the store via `pin_entity_from_arena`.

### How this integrates with MemoryStore

The typical flow is: `retrieve_pinned` → `unsafe_lazily_attach_from_arena` → MemoryStore creates `InheritedNode` → later, MemoryStore loads via `arena.load()`.

When `MemoryStore._ensure_own` encounters an InheritedNode:
- For d/n nodes: calls `parent_arena.load(node_id)`, then adopts children via `_adopt_inheritance_arena_claim` (which calls `is_tracked` and `copy_to_new_arena` on the arena — both work with the new design).
- For p/u/c/./! nodes: handled internally by `_ensure_own_derived()` — never calls `load` on the arena.

So the arena's `load` is only called for d/n nodes in this path, and the p/u load error is never hit.

### Changes to SQLiteNamespaceBackend

- **Remove `new_empty_arena()`**: no longer needed since arena is not a copy target.
- **`pin_entity_from_arena()`**: unchanged internally. Still uses `_build_tmp_arena_copying_from_external_arena` with temp DB arenas for the copy worklist.
- **`retrieve_pinned()`**: no longer creates a DB arena. Just looks up the pin, loads precomp data, returns a lightweight arena.
- **`try_lookup()`**: no longer creates a DB arena. Just checks namespace membership, loads precomp data, returns a lightweight arena.
- **`backrefs()`**: unchanged (pure read).

### Changes to sqlite_ops

**Removed/replaced functions:**
- `retrieve_pinned_and_create_arena` → new `retrieve_pinned_entity` (just returns internal_id + public_id, no arena)
- `lookup_in_namespace_and_create_arena` → new `lookup_entity_in_namespace` (just returns internal_id or None)
- `verify_in_namespace_and_add_to_arena` — no longer needed (was for `_track_from_precomputed`)
- `remove_from_arena_and_maybe_rebind` — no longer needed (was for `abandon`)
- `load_and_add_children_to_arena` → new `load_entity_cbor_and_children` (read-only: just loads CBOR and returns child internal IDs without namespace/arena writes)
- `lookup_and_add_to_arena` — no longer needed (was for `copy_from_arena`)

**Kept unchanged:**
- Session ops, namespace ops, pin ops, GC ops
- `create_arena`, `create_arena_with_entities`, `reassign_arena_to_garbage_session` (still used by internal copy mechanism)
- Copy worklist ops (`copy_discover`, `copy_save`, `copy_track_deps`, `copy_track`)
- `load_precomp_data_for_entities`
- `get_backrefs_for_entity`

### GC

No changes needed. GC already only cleans up namespace metadata (ownership, backrefs, arena membership). It never deletes entity data. With fewer DB arenas (only temp arenas from copy ops and garbage arenas), GC will actually have less work to do.

## Implementation Steps

### Phase 1: Public API changes

- [x] 1.1. Change `SQLiteNamespaceArena` base class from `CopyTargetArena` to `ReadableArena`
- [x] 1.2. Remove `copy_from_arena()` method from `SQLiteNamespaceArena`
- [x] 1.3. Remove `new_empty_arena()` from `SQLiteNamespaceBackend`
- [x] 1.4. Make `load()` raise `NotImplementedError` for non-d/n tags (p, u, c, ., !) with a descriptive error message
- [x] 1.5. Update/remove tests that use the removed API:
  - Remove `TestCopyFromArena` test class and related fixtures
  - Remove tests that call `ns.new_empty_arena()`
  - Remove `TestStoreAbandonStress` class and `stress_fixture` (exercised copy_from_arena)
  - Updated `test_u_node_storage` to verify load raises NotImplementedError
  - Keep all pin, unpin, retrieve, try_lookup, GC, session, and backref tests
- [x] 1.6. Run tests, fix any remaining failures from API removal

### Phase 2: Internal simplification of SQLiteNamespaceArena

- [x] 2.1. Remove DB arena association:
  - Remove `_arena_internal_id` field
  - Remove `_finish_init_with_internal_id()` method
  - Remove `_create_empty_in_session()` class method
  - Remove `_from_existing_db_arena()` class method
  - Replace with a simple `_create_for_entities(session, namespace, entity_internal_ids)` async factory that just loads precomp data
- [x] 2.2. Remove task scheduling queue:
  - Remove `_pending_tasks_completed_evt`, `_pending_tasks_counter`
  - Remove `_schedule_new_task()`, `_running_in_queue_slot()`, `_delay_pending_tasks()`, `_wait_for_all_pending_tasks()`
  - Remove `_db_tracked` set (no longer needed since there's no DB arena to sync with)
- [x] 2.3. Remove finalizer machinery:
  - Remove `_finalizer` field
  - Remove `_schedule_dispose_for_finalization()` class method
  - Remove `_close_for_exception()` method
- [x] 2.4. Simplify `abandon()`: just remove from `_tracked` and clean up precomp caches synchronously. No scheduled task, no DB write.
- [x] 2.5. Simplify `copy_to_new_arena()`: just create a new arena with copied precomp data. No DB arena creation, no async task. Fully synchronous.
- [x] 2.6. Simplify `close()`: just clear Python state, set `_closed = True`. No DB disposal.
- [x] 2.7. Simplify `unwrap_provenance()`, `c_node_to_context_data()`, `p_node_to_pathroot()`, `step_back_p_ctx()`: instead of calling `_track_from_precomputed` (which schedules a DB write), just add to `_tracked` directly. Remove `_track_from_precomputed()`.
- [x] 2.8. Simplify `load()` for d/n nodes:
  - Read CBOR from `stored_entity_data` via read pool
  - Extract child FxtrIDs, look up internal IDs
  - Load precomp data for children via read pool
  - Add children to `_tracked`
  - No namespace or arena membership writes
- [x] 2.9. Run tests, fix any failures

### Phase 3: Simplify retrieve_pinned, try_lookup, and sqlite_ops

- [x] 3.1. Add new sqlite_ops functions:
  - `retrieve_pinned_entity(conn, namespace_id, pin_name)` → `(entity_internal_id, entity_public_id)` (read-only, no arena)
  - `lookup_entity_in_namespace(conn, namespace_id, entity_public_id)` → `entity_internal_id | None` (read-only)
  - `load_entity_cbor_and_children(conn, internal_id)` → `(cbor_bytes, list[child_internal_id])` (read-only, for d/n nodes)
- [x] 3.2. Update `SQLiteNamespaceBackend.retrieve_pinned()` to use the new read-only ops and create a lightweight arena
- [x] 3.3. Update `SQLiteNamespaceBackend.try_lookup()` to use the new read-only ops and create a lightweight arena
- [x] 3.4. Remove unused sqlite_ops functions:
  - `retrieve_pinned_and_create_arena`
  - `lookup_in_namespace_and_create_arena`
  - `verify_in_namespace_and_add_to_arena`
  - `remove_from_arena_and_maybe_rebind`
  - `load_and_add_children_to_arena` and `_load_cbor_and_create_children` (the p/u child creation code)
  - `lookup_and_add_to_arena`
  - `_add_entity_to_arena_if_not_present` kept (still used by `copy_track_deps`)
  - `_add_entity_to_namespace_if_needed` kept (still used by `copy_track`)
- [x] 3.5. Worklist item classes kept (`_DiscoverWork`, `_SaveWork`, `_TrackWork`, `_TrackDepsWork` — still used by `_build_tmp_arena_copying_from_external_arena`)
- [x] 3.6. Run tests, fix any failures

### Phase 4: Final cleanup and documentation

- [x] 4.1. Remove dead code: any remaining unused imports, helper functions, etc.
- [x] 4.2. Update tests: verify all remaining tests pass, add a test for the p/u load error
- [x] 4.3. Run full test suite (727 tests pass)
- [x] 4.4. Update `CODE_OVERVIEW.md`
- [x] 4.5. Update `sqlite_store/LOCAL_CODE_OVERVIEW.md`

## Notes

- The `_build_tmp_arena_copying_from_external_arena` method and its worklist (`_DiscoverWork`, `_SaveWork`, etc.) are kept unchanged. They only run during `pin_entity_from_arena` and handle the actual data-copying-into-the-store use case using internal DB arenas.
- Entity reservations (`entity_reservations` table) are still used by the copy worklist to protect entities during multi-step copy operations.
- The `backrefs()` query will return fewer arena backrefs since there are fewer DB arenas, but this is expected and correct.
- Tests in `tests/deprecated/` (for the old SQLAlchemy implementation) are not touched.
