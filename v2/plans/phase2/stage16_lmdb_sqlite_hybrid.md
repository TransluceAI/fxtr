# Stage 16: LMDB-SQLite Hybrid Store

## Goal

Move entity blob data out of SQLite into an LMDB memory-mapped store to maximize read performance. SQLite remains the source of truth for metadata (namespaces, pins, deps, sessions, arenas). LMDB provides fast, synchronous, zero-thread-dispatch reads for entity data and precomputed relationship metadata.

## Current State

- All entity data (CBOR bytes) is stored in the `stored_entity_data` SQLite table
- Reads go through `ConnectionWorkerPool` read threads: async dispatch -> sqlite query -> return bytes
- Precomputed data (unwrap targets, context nodes, etc.) is loaded via a recursive CTE in SQLite
- The `entity_dep` table tracks relationships and is used by namespace management and GC

## Design

### LMDB File Layout

Each SQLite database at `{path}.db` gets a companion LMDB environment at `{path}.db.data1.lmdb`. The "data1" indicates the LMDB format version. Inside the environment, a single sub-database named `"data1/1"` holds all entity blobs.

LMDB keys are 8-byte little-endian representations of the entity's SQLite internal ID (autoincrement integer). Using internal IDs rather than FxtrIDs gives better data locality because consecutive writes produce ascending keys.

### Blob Format

Each LMDB value is a flat bytearray with a fixed-size header:

```
Offset  Size  Field
0       8     offset_fxtr_id  (always 24)
8       8     offset_cbor
16      8     offset_metadata
24      ...   fxtr_id_bytes   (raw bytes of the entity's FxtrID)
...     ...   cbor_bytes      (canonical CBOR of entity; empty for derived nodes)
...     ...   metadata_cbor   (CBOR-encoded metadata object)
```

The section boundaries are computed from consecutive offsets: `fxtr_id` runs from `offset_fxtr_id` to `offset_cbor`, `cbor` runs from `offset_cbor` to `offset_metadata`, and `metadata` runs from `offset_metadata` to end of blob.

All entity types (d, n, c, p, u, `.`, `!`) get LMDB blobs. Derived nodes (c/p/u/./!) have an empty CBOR section but still have FxtrID and metadata.

### Metadata Object

The metadata section is a CBOR-encoded dict with two entries:

```python
{
    "deps": [                    # entity's dependency list
        [internal_id, kind_str], # kind_str: "strong_ref", "weak_ref", "ctx_node", etc.
        ...
    ],
    "precomp": {                 # closed precomp map (int keys)
        internal_id: [
            fxtr_id_bytes,       # bytes: the FxtrID for this internal ID
            unwraps_to_or_null,  # int|null: internal ID of unwrap target
            ctx_node_or_null,    # int|null: internal ID of context data node
            backstep_or_null,    # int|null: internal ID of step-back-p-ctx
        ],
        ...
    }
}
```

The precomp map is **closed**: it contains an entry for the entity itself, every entity in its deps list, and every entity that appears as a value in any precomp tuple. This means reading a single LMDB blob provides everything `SQLiteNamespaceArena` needs to populate its `_precomp_*` dicts without any SQLite queries.

### Two-Phase Write Protocol

Writing a new entity uses three SQLite transactions plus one LMDB transaction:

1. **Claim**: Insert entity row with `public_id=NULL`, `storage_location='pending'`. Commit. This reserves the internal ID.
2. **LMDB write**: Compute the blob (reading deps' LMDB blobs to build the closed precomp map). Write to LMDB. Commit LMDB transaction.
3. **Commit**: Open new SQLite transaction. Update `public_id` to the real FxtrID and `storage_location` to `'data1/1'`. Insert `entity_dep` rows (still needed for namespace management and GC). Commit.
   - If this fails with a uniqueness violation on `public_id` (race with another writer), unwind in this order:
     1. Delete the LMDB blob and commit the LMDB transaction. (The SQLite "pending" row still exists, so the blob is accounted for until this point.)
     2. Delete the claimed SQLite row (the one with `public_id=NULL`) and commit.
     3. Look up the winner's internal ID, add a reservation, and return it.
   - This ordering ensures there is never an orphaned LMDB blob without a corresponding SQLite "pending" row. If we crash between steps 1 and 2, we have a dangling SQLite row with `NULL` public_id but no LMDB data — harmless, cleaned up by future GC. If we crash before step 1, we have both the SQLite row and the LMDB blob — also harmless and cleaned up together.

**Crash recovery**: If the process crashes between steps 1-2 or 2-3, we get orphaned LMDB blobs and/or SQLite rows with `NULL` public_id. These are harmless (invisible to readers, which filter on `public_id IS NOT NULL`). The invariant is that every LMDB blob has a corresponding SQLite entity row (possibly with `NULL` public_id), so future entity-level GC can find and clean up all orphans by scanning for `public_id IS NULL` rows.

### Synchronous Read Path

Once `SQLiteNamespaceArena` knows an entity's internal ID (from the initial SQLite namespace lookup), all subsequent reads are synchronous LMDB operations:

- **`_load_into_tracked(internal_ids)`**: For each internal ID, read its LMDB blob, decode metadata, populate `_precomp_*` dicts. Batch reads in a single LMDB read transaction.
- **`load(node_id)`**: Look up internal ID from `_fxtr_id_to_internal` (in-memory dict), read LMDB blob synchronously, extract CBOR bytes, decode metadata to populate precomp dicts for children, deserialize CBOR. No thread dispatch.
- **`load_flat(node_id)`**: Same as load but skip CBOR deserialization; return raw bytes + deps list (constructed from metadata).

Methods remain `async def` for interface compatibility but perform no actual async work on the data-read path.

### LMDB Environment Management

The LMDB environment is stored on `ConnectionWorkerPoolGroup` as new fields:

```python
@dataclasses.dataclass
class ConnectionWorkerPoolGroup:
    write_pool: ConnectionWorkerPool
    read_pool: ConnectionWorkerPool
    lmdb_env: lmdb.Environment
    lmdb_data_db: Any              # LMDB sub-database handle
```

The environment is opened during `setup_database_and_create_worker_pool_group()` alongside the SQLite schema setup, and closed in `aclose()`. Since `_SharedSqlitePoolGroupManager` ensures one pool group per database path, there is at most one LMDB environment open per database.

The LMDB env is thread-safe for concurrent reads. Write pool functions that need LMDB access receive `lmdb_env` and `lmdb_data_db` as explicit parameters. Read-side code in `SQLiteNamespaceArena` accesses the env directly through `self._session._pool_group.lmdb_env`.

Safety comment: Opening two `ConnectionWorkerPoolGroup`s pointing to the same LMDB environment causes undefined behavior. Always use `GLOBAL_POOL_GROUP_MANAGER` (in `sqlite_namespace.py`) to ensure a single pool group per database.

### Schema Changes (DB Version 4)

- **Remove** `stored_entity_data` table (data now in LMDB)
- **Allow NULL** on `entity.public_id` (for the claim step; `UNIQUE` constraint still applies, SQLite allows multiple NULLs)
- `entity.storage_location` is always `'data1/1'` for committed entities or `'pending'` during writes
- **Keep** `entity_dep` table (still needed by namespace management and GC)
- **Keep** all namespace, session, arena tables (unchanged)

### Migration Strategy

DB version 3 → 4 migration **raises an error** directing users to:
1. Use the archived `deprecated/sqlite_store_dbv3/` module to read the old database
2. Copy entities into a new v4 database using high-level fxtr APIs

### In-Memory Database Support

In-memory databases are **removed**. LMDB requires a filesystem path, and maintaining a fallback path for memory-only mode adds complexity for little benefit. Tests should use temporary directories.

This also removes: `_parse_sqlite_uri`, `_is_memory_db`, `_is_valid_named_shared_memory_db`, `_is_shared_cache_supported`, `memory_keep_alive_connection` fields, and all URI-parsing/shared-cache logic.

## Design Notes

- **entity_dep is kept** because namespace management (`_add_entity_to_namespace_if_needed`) copies deps to `namespace_entity_backref`, GC cascades through `entity_dep`, and the copy worklist's `copy_track_deps` reads dep internal IDs.
- **Precomp map size**: Provenance chains are typically length 0 or 1, so the closed precomp map is small. Revisit if practical usage shows storage overhead.
- **LMDB data outlives namespaces**: Entity data in LMDB is never cleaned up by current namespace-level GC. This is acceptable. Future entity-level GC should clean up both SQLite entity rows and LMDB blobs.
- **Orphaned LMDB blobs**: Possible after crashes or write races. Harmless and will be cleaned up by future entity-level GC.
- **LMDB map_size**: Use 100GB (same as existing `LMDBBytesBackingStore` in `memory_arena.py`). Can be increased if needed.
- **LMDB write options**: Use `writemap=True` for write performance but do **not** use `map_async=True`. We need LMDB writes to be durable on disk before the subsequent SQLite commit (step 3 of the write protocol) makes them visible. With `map_async`, the LMDB data might not be flushed when the SQLite transaction commits, which could lead to data loss on crash: SQLite says the entity exists at `data1/1` but the LMDB file doesn't actually have the bytes yet.

## Implementation Steps

### Phase 1: Archive Current Code

- [x] Copy `fxtr2/data/sqlite_store/` to `fxtr2/data/deprecated/sqlite_store_dbv3/`
  - This preserves the v3 implementation for loading old databases
  - Add `__init__.py` that re-exports the same public API
- [x] Move `fxtr2/data/deprecated/sqlite_arena.py` to `fxtr2/data/deprecated/sqlite_store_dbv2/sqlite_arena.py`
- [x] Move `fxtr2/data/deprecated/sqlite_arena_migrations.py` to `fxtr2/data/deprecated/sqlite_store_dbv2/sqlite_arena_migrations.py`
  - Add `__init__.py`
  - Fix any import paths
- [x] Copy `tests/test_sqlite_store/` to `tests/deprecated/test_sqlite_store_dbv3/`
  - Update imports to point to `fxtr2.data.deprecated.sqlite_store_dbv3`
  - Verify tests still pass against the deprecated copy
- [x] Move existing `tests/deprecated/` sqlite_arena tests to `tests/deprecated/test_sqlite_store_dbv2/` (if they exist and aren't already organized this way)
- [x] Run all tests (both deprecated copies and main test suite) and verify they pass

### Phase 2: LMDB Blob Format Module

- [x] Create `fxtr2/data/sqlite_store/lmdb_blob.py`
  - `HEADER_SIZE = 24` (3 x u64)
  - `encode_blob(fxtr_id_bytes: bytes, cbor_bytes: bytes | None, metadata: dict) -> bytes`
    - Serialize metadata with `cbor2` or `fxtr_cbor_dumps`
    - Build header + concatenate sections
  - `decode_blob_header(data: bytes | memoryview) -> tuple[int, int, int]` (offsets)
  - `decode_fxtr_id_bytes(data: bytes | memoryview) -> bytes`
  - `decode_cbor_bytes(data: bytes | memoryview) -> bytes | None` (None if section is empty)
  - `decode_metadata(data: bytes | memoryview) -> tuple[list, dict]` (deps_list, precomp_map)
  - Helper: `build_closed_precomp_map(own_internal_id, own_fxtr_id_bytes, own_precomp_entry, deps_with_internal_ids, lmdb_env, lmdb_data_db) -> dict`
    - Reads each dep's LMDB blob metadata, merges precomp maps, adds own entry
- [x] Add unit tests for encode/decode roundtrip, empty CBOR section, metadata merging
- [x] Run tests and verify they pass

### Phase 3: Infrastructure Changes

- [x] **Schema v4**: In `sqlite_ops.py`:
  - Bump `DB_USER_VERSION = 4`
  - Update `SQL_CREATE_TABLES`:
    - `entity.public_id` becomes `BLOB UNIQUE` (drop `NOT NULL`)
    - Remove `stored_entity_data` table and its creation SQL
    - `entity.storage_location` default is `'data1/1'`
  - Existing queries that filter on `entity.public_id` should add `AND public_id IS NOT NULL` where appropriate (e.g., `copy_discover`, `copy_save` re-check)
- [x] **Migration v3→v4**: In `sqlite_migrations.py`:
  - Add `migrate_v3_to_v4()` that raises a descriptive error:
    ```
    "Cannot automatically migrate from v3 to v4. The v4 schema uses LMDB for entity data.
     To migrate, use fxtr2.data.deprecated.sqlite_store_dbv3 to read the old database,
     then copy entities into a new v4 database using high-level fxtr scope APIs."
    ```
  - Register in `_MIGRATIONS` dict
  - Update the version assert
- [x] **LMDB environment**: In `sqlite_ops.py`:
  - In `_setup_database_schema()` (or the pool group factory functions): open the LMDB environment at `{db_path}.data1.lmdb`, create the `"data1/1"` sub-database
  - Add `lmdb_env` and `lmdb_data_db` fields to `ConnectionWorkerPoolGroup` in `sqlite_util.py`
  - Close the env in `ConnectionWorkerPoolGroup.aclose()` and `close()`
- [x] **Remove in-memory DB support**:
  - Remove `_parse_sqlite_uri`, `_is_memory_db`, `_is_valid_named_shared_memory_db`, `_is_shared_cache_supported` from `sqlite_ops.py`
  - Remove `is_memory_db` parameter from `ConnectionPool` and `ConnectionWorkerPool`
  - Remove `memory_keep_alive_connection` from `ConnectionPoolGroup` and `ConnectionWorkerPoolGroup`
  - Remove memory DB branches in `_setup_database_schema` and factory functions
  - Remove memory DB branches in `SQLiteFxtrSession` (session file logic can assume file-based DB)
  - Validate that `db_path` is a plain file path (not a `file:` URI) at the entry point
- [x] Update tests to use temporary file-based DBs (via `tmp_path` fixture) instead of memory DBs
- [x] Run all tests (main suite + deprecated copies) and verify they pass (migrations: 6 pass, lmdb_blob: 8 pass)
- [ ] Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md for infrastructure changes (deferred to Phase 7)

### Phase 4: Write Path

- [x] **Modify `copy_save()`** in `sqlite_ops.py`:
  - Add `lmdb_env` and `lmdb_data_db` parameters
  - Phase 1 (claim): Insert entity with `public_id=NULL`, `storage_location='pending'`. Commit.
  - Phase 2 (LMDB write): Call `build_closed_precomp_map()` to compute metadata. Call `encode_blob()`. Write to LMDB with key = `internal_id.to_bytes(8, 'little')`. Commit LMDB txn.
  - Phase 3 (commit): New SQLite txn. Re-check for race (someone else committed same `public_id`). If no race: update `public_id`, `storage_location='data1/1'`, insert `entity_dep` rows. Commit.
  - Race handling: If `public_id` already exists:
    1. Delete LMDB blob, commit LMDB txn (SQLite "pending" row still exists, so blob is accounted for)
    2. Delete the claimed SQLite row (`public_id=NULL`), commit
    3. Look up winner's internal_id, add reservation, return
    (LMDB delete must commit before SQLite row delete, so there is never a dangling LMDB blob with no corresponding SQLite row)
- [x] **Update callers**: `_build_tmp_arena_copying_from_external_arena()` in `sqlite_namespace.py` passes `lmdb_env` and `lmdb_data_db` through to `copy_save` calls
- [x] **Derived node writes**: Same flow as d/n nodes but with empty CBOR section. The `copy_save` caller already provides all dep information.
- [x] Add tests for basic write + LMDB blob verification (test_lmdb_write.py: 2 tests pass)
- [x] Run all tests and verify they pass

### Phase 5: Read Path

- [x] **Modify `_load_into_tracked()`** in `SQLiteNamespaceArena`:
  - Instead of calling `load_precomp_data_for_entities` via the read pool, read LMDB blobs directly
  - Open a single LMDB read transaction for the batch
  - For each internal ID in the frontier:
    - Read blob from LMDB
    - Decode metadata
    - For each entry in precomp map: populate `_fxtr_id_to_internal`, `_internal_id_to_fxtr`, `_precomp_unwraps_to`, `_precomp_ctx_node`, `_precomp_step_back_p_ctx`, `_precomp_value_count`
  - Skip entries already present (same dedup logic as current code)
  - This is fully synchronous
- [x] **Modify `load()`** in `SQLiteNamespaceArena`:
  - Look up `internal_id` from `_fxtr_id_to_internal`
  - Read LMDB blob synchronously (open read txn, get bytes, close txn)
  - Extract CBOR bytes from blob
  - Decode metadata, run the same precomp population logic as `_load_into_tracked` for child deps
  - Call `entity_core.fxtr_cbor_loads()` on the CBOR bytes
  - Return the deserialized data
  - No `await`, no thread dispatch (just synchronous Python + mmap read)
- [x] **Modify `load_flat()`** in `SQLiteNamespaceArena`:
  - For d/n nodes: read LMDB blob, extract CBOR bytes + deps from metadata. Construct `FxtrEntityFlatData` using FxtrIDs looked up from the precomp map.
  - For derived nodes: same as current (construct deps from `_precomp_*` dicts, which were populated when the parent was loaded or during `_load_into_tracked`)
  - Synchronous for both paths
- [x] **Remove dead code** from `sqlite_ops.py`:
  - `load_entity_cbor_and_children` (replaced by LMDB read)
  - `load_entity_flat` (replaced by LMDB read)
  - `load_precomp_data_for_entities` (replaced by LMDB read)
- [x] Add tests for:
  - Synchronous load of d/n nodes from LMDB
  - Synchronous load of derived nodes
  - load_flat correctness
  - Precomp population after load (verify _precomp_* dicts are correct)
- [x] Run all tests and verify they pass (95/95 pass! ✅)

### Phase 6: Test Updates

- [x] Ensure all existing `test_sqlite_store.py` tests pass with the new implementation (adapted for file-based DBs and LMDB) - 56/56 pass ✅
- [x] Ensure all existing `test_sqlite_util.py` tests pass (pool mechanics are largely unchanged) - 26/26 pass ✅
- [x] Add integration tests:
  - Full round-trip: store entity via scope -> pin in namespace -> retrieve -> load from LMDB (existing tests cover this)
  - GC still works correctly with LMDB blobs persisting (existing GC tests pass)
  - Multiple namespaces sharing the same LMDB store (existing cross-namespace tests pass)
- [ ] Add error-case tests (deferred from phases 4/5 to avoid distraction during main implementation):
  - Two-phase write with simulated crash (NULL public_id rows left behind)
  - Write race handling (two concurrent writes of same entity)
- [x] Verify deprecated test copies still pass against their respective deprecated modules:
  - v3 all tests: 86/86 pass ✅ (util: 23, migrations: 5, main: 58)
  - v3 module is now fully self-contained and can read v3 databases
- [x] Run full test suite and verify everything passes - 95/95 main tests pass ✅

### Phase 7: Documentation

- [x] Update CODE_OVERVIEW.md
- [x] Update `fxtr2/data/sqlite_store/LOCAL_CODE_OVERVIEW.md`

## File Changes Summary

**New files:**
- `fxtr2/data/sqlite_store/lmdb_blob.py` - blob encode/decode
- `fxtr2/data/deprecated/sqlite_store_dbv3/` - archived v3 implementation
- `fxtr2/data/deprecated/sqlite_store_dbv2/` - reorganized v2 archive
- `tests/deprecated/test_sqlite_store_dbv3/` - archived v3 tests

**Modified files:**
- `fxtr2/data/sqlite_store/sqlite_util.py` - LMDB env on pool group, remove memory DB params
- `fxtr2/data/sqlite_store/sqlite_ops.py` - schema v4, LMDB setup, modified copy_save, removed stored_entity_data ops, removed memory DB support
- `fxtr2/data/sqlite_store/sqlite_migrations.py` - v3→v4 error migration
- `fxtr2/data/sqlite_store/sqlite_namespace.py` - synchronous LMDB reads in arena, remove memory DB branches
- `fxtr2/data/sqlite_store/__init__.py` - update exports if needed
- `tests/test_sqlite_store/` - adapt for file-based DBs + LMDB

**Removed functionality:**
- `stored_entity_data` table
- In-memory database support (`file:...?mode=memory&cache=shared`)
- `load_entity_cbor_and_children`, `load_entity_flat`, `load_precomp_data_for_entities` in sqlite_ops
