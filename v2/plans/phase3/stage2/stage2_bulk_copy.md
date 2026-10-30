# Stage 2: Bulk Copy — Batched Entity Copy Transactions

## Context

`_build_tmp_arena_copying_from_external_arena` currently uses a four-stage worklist that executes
many small write transactions — one per entity for discovery (IMMEDIATE), save (3 txns: claim,
LMDB write, commit), track-deps (IMMEDIATE), and track (IMMEDIATE). For a tree of N entities,
this results in O(N) write transactions with O(N) fsyncs, which dominates the cost.

The goal is to batch all this work into a read-only discover sweep followed by 2-3 write
transactions total, dramatically reducing fsync overhead.

## Design Overview

Three phases replace the four-stage worklist:

1. **Discover sweep** (read-only): Walk the entity graph using the read pool. Produce a set of
   preexisting entities (already in entity table) and a topologically-ordered save queue (entities
   to write). No namespace checks — those are deferred to the big commit.

2. **Allocate IDs** (one sqlite write txn): Insert N placeholder rows (`public_id=NULL`,
   `storage_location='pending'`) to claim a contiguous range of entity IDs. Also create the
   temp arena. Commit.

3. **Big commit** (one sqlite IMMEDIATE txn + one LMDB write txn, held simultaneously):
   - (a) Walk save queue in topological order: race-check, build precomp, write LMDB blob,
     commit entity in sqlite, insert entity_dep rows. Assign allocated IDs sequentially, skipping
     raced entities.
   - (b) Commit LMDB (inner context manager exits).
   - (c) Recursive CTE from root entity through entity_dep: finds all reachable entities, bulk-adds
     those not in namespace (ownership + backrefs), and adds all to arena membership.
   - (d) Deallocate unused placeholder IDs.
   - (e) Commit sqlite (outer transaction exits).

## Key Design Decisions

**No reservations.** Entity-table GC never runs while sessions are open, so reservations are
unnecessary during the copy workflow. All reservation calls (`_add_entity_reservation`,
`_release_entity_reservation`) are removed from the copy path.

**LMDB-first commit order.** LMDB commits before sqlite. If LMDB commits but sqlite crashes,
we get harmless orphaned blobs (invisible to readers since `public_id` is still NULL). If sqlite
committed without LMDB, we'd have data loss. So LMDB-first is the correct crash-safety order.

**Range allocation with reuse.** Phase 2 allocates len(save_queue) IDs. During phase 3, raced
entities don't consume an allocated ID — the cursor just skips them. Unused IDs are deleted at
the end. This avoids maintaining a complex resolved-ID mapping.

**Precomp cache.** During the big commit, we cache each entity's closed precomp map in memory
after building it. When processing a parent, we merge its deps' cached precomp maps instead of
re-reading them from LMDB. For preexisting deps not yet cached, we read from LMDB within the
write txn (which sees both committed data and our own writes).

**Read pool for discovery.** The discover sweep only needs SELECT queries. Using the read pool
(DEFERRED txns) avoids acquiring write locks and allows concurrent readers.

**DFS gives topological order for free.** The discover sweep uses a DFS worklist (stack). When
an entity is not in the entity table, we push an `_EnqueueSave` item first, then push `Discover`
for each dep. Deps are processed (popped) before the parent's `_EnqueueSave`, so they get added
to `save_queue` first. The resulting `save_queue` is topologically sorted by construction — no
separate sort step needed.

**CTE for namespace + arena membership.** Instead of checking namespace membership during the
discover sweep, we defer all namespace work to a recursive CTE in the big commit. This is
correct even if namespace membership changes between discover and commit (since we hold the
write lock during the CTE). The CTE walks entity_dep from the root entity, finds ALL
transitively-reachable entities, and bulk-adds any not yet in the namespace. It also bulk-adds
all reachable entities to the arena. This means the discover sweep doesn't need to recurse into
preexisting entities' deps at all — once we find something in the entity table, we stop.

## Implementation Details

### Phase 0: Discover Sweep

New method `_discover_sweep_for_copy` on `SQLiteNamespaceBackend`, called from
`_build_tmp_arena_copying_from_external_arena`.

```python
# Discover sweep outputs:
save_queue: list[SaveItem]           # topologically ordered (deps first) — built during DFS
preexisting: dict[FxtrID, int]       # fxtr_id -> internal_id
```

Two work item types:

- **_DiscoverWork(fxtr_id)**: Query entity table by public_id (read pool).
  - Found → add to `preexisting`. **Stop** (don't recurse — CTE handles transitive deps).
  - Not found → `await source.load_flat(fxtr_id)`. Push `_EnqueueSaveWork(save_item)` first,
    then push `_DiscoverWork` for each dep. Deps get popped before the enqueue item (LIFO),
    so they're added to save_queue before the parent.

- **_EnqueueSaveWork(save_item)**: Append `save_item` to `save_queue`. That's it.

The DFS stack naturally produces topological order: deps are always enqueued before parents.
No separate sort step needed.

This is simpler than the old four-stage worklist: no namespace checks, no TrackDeps. Just
"is it in the entity table or not?"

Read-only query helper in `sqlite_ops.py`:

```python
def discover_entity_by_public_id(conn, public_id: bytes) -> int | None:
    """Return internal_id if entity exists (committed), else None. Read-only."""
    row = conn.execute(
        "SELECT id FROM entity WHERE public_id = ?", (public_id,)
    ).fetchone()
    return row[0] if row else None
```

### Phase 1: Allocate + Create Arena

New function in `sqlite_ops.py`:

```python
def bulk_allocate_and_create_arena(
    conn, count: int, arena_public_id: bytes, namespace_id: int, session_id: int,
) -> tuple[list[int], int]:
    """Allocate `count` entity ID placeholders and create an empty arena.

    Single IMMEDIATE transaction. Returns (allocated_ids, arena_id).
    """
    with transaction(conn, "IMMEDIATE"):
        allocated_ids = []
        for _ in range(count):
            cursor = conn.execute(
                "INSERT INTO entity (public_id, storage_location) VALUES (NULL, 'pending')"
            )
            allocated_ids.append(cursor.lastrowid)

        cursor = conn.execute(
            "INSERT INTO arena (public_id, namespace_id, session_id) VALUES (?, ?, ?)",
            (arena_public_id, namespace_id, session_id),
        )
        arena_id = cursor.lastrowid

    return allocated_ids, arena_id
```

### Phase 2: Big Commit

New function in `sqlite_ops.py`. This is the core of the optimization.

```python
def bulk_copy_commit(
    conn: sqlite3.Connection,
    namespace_id: int,
    arena_id: int,
    root_internal_id: int,              # internal_id of root entity (known after save queue)
    preexisting: dict[bytes, int],      # public_id_bytes -> internal_id
    save_queue: list[SaveItem],         # topologically ordered (deps first)
    allocated_ids: list[int],           # pre-allocated internal IDs
    lmdb_env, lmdb_data_db,
) -> dict[bytes, int]:
    """Execute the big commit: save entities, add to namespace via CTE, add to arena."""
```

**Step A: Process save queue** (inside both sqlite IMMEDIATE txn and nested LMDB write txn):

```python
with transaction(conn, "IMMEDIATE"):
    id_cursor = 0
    resolved = dict(preexisting)  # public_id_bytes -> resolved internal_id
    precomp_cache: dict[int, dict[int, list]] = {}

    with lmdb_env.begin(db=lmdb_data_db, write=True) as lmdb_txn:
        for item in save_queue:
            # Race check
            row = conn.execute(
                "SELECT id FROM entity WHERE public_id = ? AND public_id IS NOT NULL",
                (item.public_id_bytes,),
            ).fetchone()

            if row is not None:
                # Raced — use winner's ID
                resolved[item.public_id_bytes] = row[0]
                continue

            # Assign next allocated ID
            internal_id = allocated_ids[id_cursor]
            id_cursor += 1

            # Resolve dep internal IDs from resolved map
            deps_internal = [
                (resolved[dep_pub_id], kind)
                for dep_pub_id, kind in item.deps_as_bytes
            ]

            # Build precomp (using cache + LMDB reads from same txn)
            own_precomp_entry = _build_own_precomp_entry(item.public_id_bytes, deps_internal)
            precomp_map = _build_precomp_cached(
                internal_id, own_precomp_entry, deps_internal,
                precomp_cache, lmdb_txn
            )

            # Encode and write LMDB blob
            metadata = {
                "deps": [[iid, kind] for iid, kind in deps_internal],
                "precomp": precomp_map,
            }
            blob = lmdb_blob.encode_blob(item.public_id_bytes, item.cbor_bytes, metadata)
            lmdb_txn.put(internal_id.to_bytes(8, "little"), blob)

            # Commit entity in sqlite (set public_id, insert entity_dep rows)
            conn.execute(
                "UPDATE entity SET public_id = ?, storage_location = 'data1/1' WHERE id = ?",
                (item.public_id_bytes, internal_id),
            )
            conn.executemany(
                "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (?, ?, ?)",
                [(internal_id, dep_iid, kind) for dep_iid, kind in deps_internal],
            )

            resolved[item.public_id_bytes] = internal_id
            precomp_cache[internal_id] = precomp_map

        # LMDB commits here (inner context manager exit)
```

**Step B: CTE for namespace + arena membership** (still inside sqlite txn, after LMDB committed):

The root entity's internal_id is known: either from `preexisting` (if it was already in the
entity table) or from `resolved` (if it was just saved). Use a recursive CTE to find all
entities reachable from the root via entity_dep.

```python
    root_iid = resolved[root_public_id_bytes]

    # Use temp table to store CTE results (reused for ownership, backrefs, arena)
    conn.execute("CREATE TEMP TABLE _bulk_reachable (entity_id INTEGER PRIMARY KEY)")
    conn.execute("""
        INSERT INTO _bulk_reachable (entity_id)
        WITH RECURSIVE reachable(entity_id) AS (
            SELECT ?
            UNION
            SELECT ed.required_id
            FROM entity_dep ed
            JOIN reachable ON ed.requirer_id = reachable.entity_id
        )
        SELECT entity_id FROM reachable
    """, (root_iid,))

    # Find which reachable entities need to be added to namespace
    conn.execute("""
        CREATE TEMP TABLE _bulk_ns_add AS
        SELECT entity_id FROM _bulk_reachable
        WHERE NOT EXISTS (
            SELECT 1 FROM namespace_entity_ownership
            WHERE namespace_id = ? AND entity_id = _bulk_reachable.entity_id
        )
    """, (namespace_id,))

    # Bulk add to namespace_entity_ownership
    conn.execute("""
        INSERT INTO namespace_entity_ownership (namespace_id, entity_id)
        SELECT ?, entity_id FROM _bulk_ns_add
    """, (namespace_id,))

    # Bulk add backrefs for newly-added namespace entities
    conn.execute("""
        INSERT OR IGNORE INTO namespace_entity_backref
            (namespace_id, requirer_id, required_id, kind)
        SELECT ?, ed.requirer_id, ed.required_id, ed.kind
        FROM entity_dep ed
        WHERE ed.requirer_id IN (SELECT entity_id FROM _bulk_ns_add)
    """, (namespace_id,))

    # Bulk add all reachable entities to arena
    conn.execute("""
        INSERT OR IGNORE INTO arena_membership (arena_id, entity_id)
        SELECT ?, entity_id FROM _bulk_reachable
    """, (arena_id,))

    # Cleanup temp tables
    conn.execute("DROP TABLE _bulk_ns_add")
    conn.execute("DROP TABLE _bulk_reachable")
```

**Step C: Deallocate unused IDs** (still inside sqlite txn):

```python
    for i in range(id_cursor, len(allocated_ids)):
        conn.execute("DELETE FROM entity WHERE id = ?", (allocated_ids[i],))

    # sqlite commits here (outer transaction context manager exit)
```

### Precomp Cache Helper

New function in `lmdb_blob.py`:

```python
def build_closed_precomp_map_cached(
    own_internal_id: int,
    own_precomp_entry: list,
    deps_with_internal_ids: list[tuple[int, str]],
    precomp_cache: dict[int, dict[int, list]],
    lmdb_txn,                    # existing LMDB txn for reading
) -> dict[int, list]:
    """Like build_closed_precomp_map but uses in-memory cache, falling back to LMDB reads."""
    result = {own_internal_id: own_precomp_entry}
    for dep_iid, _ in deps_with_internal_ids:
        if dep_iid in result:
            continue
        if dep_iid in precomp_cache:
            dep_precomp = precomp_cache[dep_iid]
        else:
            blob = lmdb_txn.get(dep_iid.to_bytes(8, "little"))
            _, dep_precomp = decode_metadata(blob)
            precomp_cache[dep_iid] = dep_precomp
        for iid, entry in dep_precomp.items():
            if iid not in result:
                result[iid] = entry
    return result
```

Existing `build_closed_precomp_map` is left unchanged for its existing callers (tests, etc.).

### Preexisting-only fast path

When save_queue is empty (all entities already in DB), we skip the allocate + LMDB phases and
just need a single sqlite write txn that creates the arena and runs the CTE for namespace/arena
membership. New function `bulk_copy_commit_preexisting_only` in `sqlite_ops.py`.

### Refactored `_build_tmp_arena_copying_from_external_arena`

The main method becomes a thin orchestrator:

```python
async def _build_tmp_arena_copying_from_external_arena(self, entity_id, source):
    # Phase 0: Discover sweep (read pool, no namespace checks)
    # save_queue is already topologically sorted by the DFS walk
    save_queue, preexisting = await self._discover_sweep_for_copy(entity_id, source)
    source.close()

    if not save_queue:
        # Only preexisting entities — single sqlite write txn
        arena_id = await self._pool.write_pool.run_sync_with_connection(
            sqlite_ops.bulk_copy_commit_preexisting_only,
            self._namespace_internal_id, arena_uuid.bytes, session_id,
            preexisting[entity_id],  # root internal_id
        )
        return arena_id, preexisting[entity_id]

    # Phase 1: Allocate IDs + create arena (one write txn)
    allocated_ids, arena_id = await self._pool.write_pool.run_sync_with_connection(
        sqlite_ops.bulk_allocate_and_create_arena,
        len(save_queue), arena_uuid.bytes, self._namespace_internal_id, session_id,
    )

    # Phase 2: Big commit (one write txn + one LMDB txn)
    resolved = await self._pool.write_pool.run_sync_with_connection(
        sqlite_ops.bulk_copy_commit,
        self._namespace_internal_id, arena_id,
        entity_id._raw_data_id,  # root public_id for resolving root internal_id
        {fid._raw_data_id: iid for fid, iid in preexisting.items()},
        save_queue, allocated_ids,
        lmdb_env, lmdb_data_db,
    )

    return arena_id, resolved[entity_id._raw_data_id]
```

## Files Modified

- **`sqlite_ops.py`**: Add `discover_entity_by_public_id`, `bulk_allocate_and_create_arena`,
  `bulk_copy_commit`, `bulk_copy_commit_preexisting_only`. Remove old `copy_discover`,
  `copy_save`, `copy_track_deps`, `copy_track`.

- **`sqlite_namespace.py`**: Rewrite `_build_tmp_arena_copying_from_external_arena` as thin
  orchestrator. Add `_discover_sweep_for_copy` method. Replace `_DiscoverWork`, `_SaveWork`,
  `_TrackWork`, `_TrackDepsWork` with `SaveItem`, `_DiscoverWork`, `_EnqueueSaveWork`.

- **`lmdb_blob.py`**: Add `build_closed_precomp_map_cached` alongside existing function.

- **`LOCAL_CODE_OVERVIEW.md`**: Update copy operations section to describe new batched protocol.

## Tests

Existing tests in `test_sqlite_store.py` cover the copy workflow end-to-end (cross-namespace
copying, pinning from external arena, etc.). These should continue to pass with no modifications,
validating correctness of the refactor.

Additional targeted tests:

- **`test_discover_sweep_all_preexisting`**: All entities already in DB. Verify empty save queue,
  correct preexisting set.

- **`test_discover_sweep_all_new`**: No entities in DB. Verify save queue has all entities in
  topological order, empty preexisting set.

- **`test_discover_sweep_mixed`**: Some preexisting, some new. Verify correct partitioning.

- **`test_bulk_commit_race_handling`**: Pre-commit one entity, then bulk-commit a queue that
  includes it. Verify raced entity uses winner's ID, non-raced entities succeed, unused allocated
  IDs are cleaned up.

- **`test_bulk_commit_precomp_cache`**: Verify precomp maps are correct for a chain of entities
  (A depends on B depends on C), using the cached-precomp path.

- **`test_discover_sweep_topological_order`**: Diamond dependency (A->B, A->C, B->D, C->D).
  Verify save_queue has D before B and C, which come before A.

- **`test_cte_namespace_add`**: Preexisting entity with deps not in namespace. Verify CTE
  recursively adds all transitive deps to namespace.

## Implementation Steps

- [x] Write scratch notes to `plans/phase3/stage2_bulk_copy_scratch.md`
- [x] Copy this plan to `plans/phase3/stage2_bulk_copy.md`
- [x] Add `discover_entity_by_public_id` to `sqlite_ops.py`
- [x] Add `build_closed_precomp_map_cached` to `lmdb_blob.py`
- [x] Add `SaveItem`, `_DiscoverWork`, `_EnqueueSaveWork` dataclasses to `sqlite_namespace.py`
- [x] Implement `_discover_sweep_for_copy` in `sqlite_namespace.py`
- [x] Add `bulk_allocate_and_create_arena` to `sqlite_ops.py`
- [x] Add `bulk_copy_commit` to `sqlite_ops.py` (save queue + CTE namespace/arena)
- [x] Add `bulk_copy_commit_preexisting_only` to `sqlite_ops.py` (fast path)
- [x] Rewrite `_build_tmp_arena_copying_from_external_arena` as thin orchestrator
- [x] Remove old worklist dataclasses and old `copy_discover`/`copy_save`/`copy_track_deps`/`copy_track`
- [x] Run existing tests, fix any issues
- [x] Add new targeted tests
- [x] Update `LOCAL_CODE_OVERVIEW.md`
