# Replace LMDB with Append-Only Segment Files

## Context

The sqlite_store module uses LMDB as a companion blob store alongside SQLite. Entity blobs (CBOR + metadata) are stored in a memory-mapped LMDB file keyed by internal ID, while SQLite stores metadata, deps, namespaces, pins, and sessions.

**Problem**: Writes are bottlenecked on LMDB commits. LMDB's B-tree page msync is expensive, and we're not using LMDB's features (random-access updates, MVCC) — we only ever append immutable data. A sequential append-only log is a better fit.

**Solution**: Replace LMDB with append-only segment files. Sequential `write()` + single `fdatasync()` replaces scattered B-tree page flushing. SQLite tracks where each entity lives in which segment file.

**Database version**: 5 → 6.

## Design

### Segment Files

Segment files live in `{db_path}.data2.shards/` (matching the `data1` convention for LMDB). Files are named `seg_{N:08d}.bin` with monotonically increasing N (never reused, even after future compaction). At most one segment is "active" (being appended to) at a time.

### Record Envelope

Each record in a segment file:

```
[8-byte LE record_length][blob_bytes ...]
```

`blob_bytes` uses the same format as current LMDB blobs (24-byte offset header + fxtr_id + cbor + metadata_cbor). The 8-byte length prefix makes records self-describing — readers that have only an offset (e.g. from a same-segment precomp pointer) can determine the blob size without a SQLite lookup.

### Extended Precomp Entry

Precomp entries in blob metadata gain an optional 5th element:

```
[fxtr_id_bytes, unwraps_to_or_null, ctx_node_or_null, backstep_or_null, seg_offset_or_null]
```

`seg_offset_or_null` points to the start of the record envelope for that entity within the **same** segment file. Null means the dep is in a different segment (readers must do a SQLite lookup). During bulk writes, deps written in the same batch naturally get same-segment offsets since they go to the same active segment.

### SQLite Schema Changes

New table for segment lifecycle:

```sql
CREATE TABLE segment_metadata (
    segment_id INTEGER PRIMARY KEY,
    status VARCHAR NOT NULL DEFAULT 'active',  -- 'active' or 'sealed'
    write_cursor INTEGER NOT NULL DEFAULT 0,   -- next-write byte offset (committed)
    created_at DATETIME NOT NULL
)
```

New columns on the existing `entity` table (instead of a separate join table — it's a 1:1 relationship, and inlining avoids a JOIN on every read):

```sql
ALTER TABLE entity ADD COLUMN seg_id INTEGER;     -- segment file number
ALTER TABLE entity ADD COLUMN seg_offset INTEGER;  -- byte offset of record envelope start
ALTER TABLE entity ADD COLUMN seg_length INTEGER;  -- byte length of blob (excl. 8-byte envelope header)
```

These columns are NULL for entities not yet migrated or during the `'pending'` phase. After migration, all entities have non-NULL values. Queries that already touch the `entity` table (e.g. `retrieve_pinned_entity`, `lookup_entity_in_namespace`) can return segment location data for free, eliminating a separate lookup on the read path.

`entity.storage_location` gets a new value `'data2/seg'` for segmented entities. Existing `'data1/1'` (LMDB) value exists only during migration. The `'pending'` placeholder value is no longer needed — since we allocate internal IDs on the fly (see Phase 3), entity rows are inserted with their final `storage_location` immediately.

### Crash Safety

Same ordering principle as the current LMDB approach:

1. Append records to segment file
2. `fdatasync()` the segment file — data is durable on disk
3. Commit SQLite transaction — makes data visible to readers

If we crash between steps 2 and 3, the segment has trailing data that SQLite doesn't know about. On startup, we read `write_cursor` from `segment_metadata` for the active segment and truncate the file to that position.

### Write Locking

A `write.lock` file in `{db_path}.data2.shards/` is `flock(LOCK_EX)`'d during writes. This is belt-and-suspenders with SQLite's IMMEDIATE transaction for multi-process safety. Readers never acquire this lock — they use `pread` at offsets already committed in SQLite, which are guaranteed flushed.

### FD Cache

A `SegmentManager` class holds an LRU cache of open file descriptors for segment files (default capacity: 16). Reads use `os.pread()` which is thread-safe (no shared seek pointer). Sealed segments are immutable so their fds are safe to cache indefinitely.

### Segment Rotation

Default max segment size: 512 MB (soft limit). After a batch completes writing, if the segment file exceeds this size, it is sealed (`status='sealed'`) and a new active segment is created. A single batch can exceed the limit.

---

## Phase 1: Foundation — New Modules and Schema

Create new files and update schema definitions. No runtime behavior change — all existing tests pass.

**Files created**: `entity_blob.py`, `segment_io.py`
**Files modified**: `sqlite_ops.py` (schema constants only)

- [x] **1a. Create `entity_blob.py`**

  Copy from `lmdb_blob.py`: `encode_blob`, `decode_blob_header`, `decode_fxtr_id_bytes`, `decode_cbor_bytes`, `decode_metadata`, and constants (`HEADER_SIZE`, `_HEADER_STRUCT`). These are pure byte-manipulation with zero LMDB dependency.

  Add record envelope helpers: `RECORD_ENVELOPE_HEADER = struct.Struct("<Q")`, `encode_record_envelope(blob: bytes) -> bytes`.

  Do NOT copy `build_closed_precomp_map` or `build_closed_precomp_map_cached` — those are adapted in Phase 3.

- [x] **1b. Create `segment_io.py`**

  `SegmentManager` class:
  - `__init__(segments_dir, max_cache_size=16)` — holds `segments_dir: str`, `_fd_cache: dict[int, int]` (segment_id → fd), LRU eviction
  - `segment_path(segment_id) -> str` — returns `"{segments_dir}/seg_{segment_id:08d}.bin"`
  - `read_record(segment_id, offset, length) -> bytes` — `pread` from cached fd; reads `length` bytes at `offset + 8` (skipping envelope header)
  - `read_record_at(fd_or_segment_id, offset) -> bytes` — reads envelope header first (8 bytes) to get length, then reads blob; for same-segment precomp fast path where caller only has offset
  - `close()` — close all cached fds

  Module-level helpers:
  - `append_record(fd, blob) -> tuple[int, int]` — writes `[8-byte LE len][blob]`, returns `(offset_of_envelope_start, len(blob))`
  - `flush_segment(fd)` — platform-aware durable flush: `os.fdatasync(fd)` on Linux; `fcntl.fcntl(fd, fcntl.F_FULLFSYNC)` on macOS (plain `fsync` on macOS only flushes to the drive's write cache, not to disk)
  - `truncate_to_cursor(path, write_cursor)` — truncate file to committed cursor position

  Design note: `pread` is thread-safe (no shared seek pointer), so multiple reader threads can share the same cached fd without locking.

- [x] **1c. Add segment schema constants to `sqlite_ops.py`**

  Add `SQL_CREATE_SEGMENT_TABLES` list with `CREATE TABLE segment_metadata` and `ALTER TABLE entity ADD COLUMN` statements for `seg_id`, `seg_offset`, `seg_length`. For fresh databases, add the columns directly in the `entity` CREATE TABLE in `SQL_CREATE_TABLES`. The ALTER TABLE form is used only by the v6 migration.

  Remove `entity_reservations` from `SQL_CREATE_TABLES` (it's unused; the comment says "TODO: clean up next time we do a database migration"). The DROP goes in the migration instead.

  Do NOT bump `DB_USER_VERSION` yet — that happens in Phase 2.

- [x] Update LOCAL_CODE_OVERVIEW.md: document `entity_blob.py` and `segment_io.py`

- [x] **1d. Create `test_entity_blob.py`**

  Adapt from `test_lmdb_blob.py`. The 4 pure encode/decode tests (`TestBlobEncodeDecode`) transfer with just an import change. Add tests for `encode_record_envelope`. (The 4 `TestBuildClosedPrecompMap` tests are deferred to Phase 3 when `build_closed_precomp_map_for_segments` is written.)

- [x] **1e. Create `test_segment_io.py`**

  Unit tests for `SegmentManager`:
  - `append_record` writes correct envelope format
  - `read_record` reads back correctly
  - `flush_segment` + `truncate_to_cursor` crash recovery roundtrip
  - FD cache: opens, caches, evicts oldest on overflow
  - `close` closes all fds
  - `read_record_at` (envelope-header-based) works correctly

**Test milestone**: All existing tests still pass (no runtime changes). New `test_entity_blob.py` and `test_segment_io.py` unit tests pass.

---

## Phase 2: Migration (v5 → v6)

Write the migration function that reads all LMDB data and writes it to a segment file.

**Files modified**: `sqlite_migrations.py`, `sqlite_ops.py`, `sqlite_util.py`

- [x] **2a. Write `migrate_v5_to_v6` in `sqlite_migrations.py`**

  The migration function receives a `sqlite3.Connection`. It:

  1. Creates `segment_metadata` table and adds `seg_id`/`seg_offset`/`seg_length` columns to `entity`
  2. Drops `entity_reservations` table
  3. Resolves db_path from connection (`PRAGMA database_list`)
  4. Creates `{db_path}.data2.shards/` directory
  5. Creates segment file `seg_00000000.bin`
  6. Inserts row into `segment_metadata` (segment_id=0, status='active', write_cursor=0)
  7. Opens the LMDB environment (same path detection as current code: `.data1.mdb` or `.data1.lmdb/`)
  8. Iterates all entities with `storage_location = 'data1/1'` ordered by `id ASC`:
     - Reads blob from LMDB (key = `id.to_bytes(8, 'little')`)
     - Decodes metadata, rewrites precomp entries with same-segment offsets (5th element) for deps already written to this segment
     - Re-encodes blob, wraps in record envelope, appends to segment file
     - Updates entity: `SET storage_location='data2/seg', seg_id=?, seg_offset=?, seg_length=?`
  9. Flushes segment file
  10. Updates `segment_metadata.write_cursor`
  11. Closes LMDB env
  12. Sets `PRAGMA user_version = 6`

  Note: LMDB file deletion is deferred to Phase 5 (so files remain available during development/testing of earlier phases).

  **Same-segment offset trick**: We iterate in `id ASC` order. Since the bulk_copy protocol always inserts deps before their dependents, lower IDs are always deps of higher IDs. As we write each entity, we record `migrated_offsets[internal_id] = offset`. When building the new precomp for entity E, we look up each dep's internal_id in `migrated_offsets` and add the offset as the 5th element. Every dep is guaranteed to already be in `migrated_offsets`.

  **Import note**: The migration imports `lmdb` and uses `lmdb_blob.decode_metadata` directly. This is the only runtime path that touches LMDB after v6.

  **Implementation note**: The ALTER TABLE statements for adding seg columns live directly in the migration function (not as a shared constant in `sqlite_ops.py`), since they're only used during migration.

- [x] **2b. Register migration**: Add `(5, 6): migrate_v5_to_v6` to `_MIGRATIONS` dict.

- [x] **2c. Bump `DB_USER_VERSION` to 6** in `sqlite_ops.py`.

- [x] **2d. Update assert** in `sqlite_migrations.py` to `assert DB_USER_VERSION == 6`.

- [x] **2e. Update `_setup_database_schema` in `sqlite_ops.py`**

  For fresh databases (version 0):
  - Execute `SQL_CREATE_TABLES` + `SQL_CREATE_SEGMENT_TABLES`
  - Create `{db_path}.data2.shards/` directory and initial segment file
  - Insert initial `segment_metadata` row
  - Set version to 6
  - Do NOT open LMDB

  For existing databases: run migrations as before (v5→v6 migration handles segment setup).

  After migration/creation:
  - Create `SegmentManager` for the segments directory
  - Perform crash recovery: read active segment's `write_cursor`, truncate file if needed
  - Return signature changes to `(segment_manager, setup_conn)` (no more `lmdb_env, lmdb_data_db`)

  **LMDB opening removed**: After v6, no entities have `storage_location = 'data1/1'`, so LMDB is never opened at runtime.

- [x] **2f. Update `ConnectionWorkerPoolGroup` in `sqlite_util.py`**

  Replace `lmdb_env: Any` and `lmdb_data_db: Any` with `segment_manager: SegmentManager` and `segments_dir: str`. Update `close()` and `aclose()` to call `segment_manager.close()`. Remove LMDB safety comments (segment files don't have the "one env per file" constraint).

  This is done early (rather than in Phase 5) because Phases 3-4 need `pool_group.segment_manager` to exist.

- [x] **2g. Update `setup_database_and_create_worker_pool_group` in `sqlite_ops.py`**

  After `_setup_database_schema` returns `(segment_manager, setup_conn)`, pass `segment_manager` and `segments_dir` to the `ConnectionWorkerPoolGroup` constructor.

- [x] Update LOCAL_CODE_OVERVIEW.md: document migration

- [x] **2h. Add `test_v5_to_v6_migration` to `test_sqlite_migrations.py`**

  Three tests:
  1. `test_migration_with_entities`: Create v5 DB with LMDB data (2 entities with dep), run migration, verify segment files, blob content, precomp 5th elements
  2. `test_migration_no_entities`: Empty v5 DB, verify empty segment file created
  3. `test_migration_entity_reservations_dropped`: Verify entity_reservations table dropped even with data

- [x] **2i. Update `TestDatabaseInitialization` and `TestLmdbNosubdir` in `test_sqlite_store.py`**

  - `TestLmdbNosubdir` → renamed to `TestSegmentFiles`. Tests: fresh DB creates `.data2.shards/` directory with `seg_00000000.bin`, segment_metadata row exists, DB can be reopened.
  - `TestDatabaseInitialization::test_memory_db_not_supported`: updated error message.
  - `TestDatabaseInitialization::test_database_user_version`: passes via DB_USER_VERSION import (auto-updated to 6).

**Test milestone**: 77 tests pass (Phase 1 unit tests + migration tests + fresh-DB + segment-files + session lifecycle + GC). 44 tests fail as expected (write/read paths still reference LMDB).

---

## Phase 3: Write Path

Replace LMDB writes with segment file writes. Also simplify the two-transaction allocate-then-commit pattern into a single transaction.

**Key simplification**: The old code used `bulk_allocate_and_create_arena` (IMMEDIATE txn #1) to pre-allocate placeholder entity rows, then `bulk_copy_commit` (IMMEDIATE txn #2) to fill them in. This pre-allocation was necessary because LMDB keys were internal IDs that had to be known before writing blobs. With segments, the write_cursor serves the reservation purpose — we can allocate internal IDs on the fly by INSERTing entity rows as we go. This merges the two transactions into one, eliminating:
- The `bulk_allocate_and_create_arena` function
- The `allocated_ids` parameter
- The unused-ID cleanup step at the end

**Files modified**: `sqlite_ops.py`, `entity_blob.py`, `sqlite_namespace.py`

- [x] **3a. Add `build_closed_precomp_map_for_segments` to `entity_blob.py`**

  Replaces `build_closed_precomp_map_cached` from `lmdb_blob.py`. Instead of `lmdb_txn`, takes a generic callback:

  ```python
  def build_closed_precomp_map_for_segments(
      own_internal_id: int,
      own_precomp_entry: list,
      deps_with_internal_ids: list[tuple[int, str]],
      precomp_cache: dict[int, dict[int, list]],
      read_blob: Callable[[int], bytes],
  ) -> dict[int, list]:
  ```

  Logic is identical to the cached LMDB version: check `precomp_cache` first, fall back to `read_blob(dep_internal_id)` → `decode_metadata()`, populate cache.

- [x] **3b. Rewrite `bulk_copy_commit` in `sqlite_ops.py`**

  New signature: replace `lmdb_env`/`lmdb_data_db` with `segment_manager: SegmentManager` and `segments_dir: str`. Remove `allocated_ids` parameter. The function now allocates IDs on the fly.

  New flow within a single IMMEDIATE transaction:

  1. Acquire `flock(LOCK_EX)` on `{segments_dir}/write.lock`
  2. Read active segment from `segment_metadata` (segment_id, write_cursor)
  3. Open segment file, `os.lseek` to `write_cursor`
  4. Create arena row (previously done in `bulk_allocate_and_create_arena`)
  5. Pre-fetch segment locations for all preexisting deps: `SELECT id, seg_id, seg_offset, seg_length FROM entity WHERE id IN (...)` (one batch SELECT)
  6. Build a `read_blob` callback: checks precomp_cache first; for preexisting deps, uses pre-fetched locations + `segment_manager.read_record()`; for same-batch deps already written, uses `os.pread` on the open segment fd
  7. For each item in topological save_queue:
     - Race check: `SELECT id FROM entity WHERE public_id = ?` — if already exists, use winner's ID, skip write
     - Otherwise: `INSERT INTO entity (public_id, storage_location) VALUES (?, 'data2/seg')` — get `internal_id` from `lastrowid` (seg columns are NULL at this point)
     - Build own_precomp_entry (5 elements); for deps in same batch/segment, set seg_offset to their offset, otherwise null
     - `build_closed_precomp_map_for_segments(...)` with `read_blob` callback
     - `encode_blob(...)`, `append_record(fd, blob)` → get `(offset, length)`
     - `UPDATE entity SET seg_id=?, seg_offset=?, seg_length=? WHERE id=?` — fill in segment location now that we know it
     - INSERT `entity_dep` rows
     - Record (internal_id, offset) for same-batch dep resolution
  8. `flush_segment(fd)` — fdatasync
  9. UPDATE `segment_metadata` SET `write_cursor` to new position
  10. If file size > max_segment_size (configurable, default 512 MB): UPDATE status='sealed', INSERT new active segment row, create new file
  11. Release flock
  12. CTE for namespace/arena membership
  13. SQLite COMMIT (outer transaction context manager exit)

  **Crash safety**: Data is flushed to segment file (step 8) before SQLite commits (step 13). If crash occurs between them, crash recovery on next startup truncates the segment to the committed write_cursor.

- [x] **3c. Remove `bulk_allocate_and_create_arena`** from `sqlite_ops.py` (or keep it for any other callers but remove usage from the segment write path).

- [x] **3d. Update `_build_tmp_arena_copying_from_external_arena`** in `sqlite_namespace.py`: the three-phase discover→allocate→commit becomes two-phase discover→commit. Remove the `bulk_allocate_and_create_arena` call. Pass `pool_group.segment_manager` and `pool_group.segments_dir` to the new `bulk_copy_commit`.

- [x] **3e. Update `bulk_copy_commit_preexisting_only`**: This fast path (all entities already exist, no new writes) doesn't touch LMDB and needs only trivial changes — remove any LMDB parameters from the signature if present.

- [x] Update LOCAL_CODE_OVERVIEW.md: document new write path

- [x] **3f. Create `test_segment_write.py`**

  Replace `test_lmdb_write.py`. After `bulk_copy_commit`, query entity `seg_id/seg_offset/seg_length` columns, pread from the segment file, decode and verify blob contents. Same entity structures as before (d-node with ctx dep, derived node with empty CBOR).

- [x] **3g. Add `build_closed_precomp_map_for_segments` tests to `test_entity_blob.py`**

  The 4 `TestBuildClosedPrecompMap` tests from `test_lmdb_blob.py` are rewritten to test `build_closed_precomp_map_for_segments` with a mock `read_blob` callback (no LMDB env needed).

**Test milestone**: 100 tests pass, 33 fail (read-path `lmdb_env` references + 2 old LMDB write tests). Segment write tests (5) pass. Precomp map tests (5) pass. +23 tests compared to Phase 2.

---

## Phase 4: Read Path

Replace LMDB reads in arena methods with segment file reads. Also enable same-segment precomp fast-path reads — when a precomp entry has a non-null 5th element (seg_offset), the reader can jump directly to that offset in the same segment file without a SQLite lookup.

**Files modified**: `sqlite_ops.py`, `sqlite_namespace.py`

- [x] **4a. Update upstream queries to return segment locations** in `sqlite_ops.py`

  `retrieve_pinned_entity` and `lookup_entity_in_namespace` already JOIN on the `entity` table. Add `e.seg_id, e.seg_offset, e.seg_length` to their SELECT lists. This is the main win of inlining segment columns on `entity` — callers get locations for free without a separate query.

  Update return types to include the segment location tuple alongside internal_id/public_id. Callers in `sqlite_namespace.py` pass these through to arena creation, pre-populating `_segment_locations`.

- [x] **4b. Add segment location caching to `SQLiteNamespaceArena`**

  Add `_segment_locations: dict[int, tuple[int, int, int]]` field (maps `internal_id → (segment_id, offset, length)`). Pre-populated from upstream query results (4a) and during `_load_into_tracked`.

- [x] **4c. Rewrite `_load_into_tracked`**

  Current: opens one LMDB read txn, reads blob for each internal_id by key.

  New:
  1. For any `frontier_list` internal_ids not already in `_segment_locations`, dispatch a read-pool query: `SELECT id, seg_id, seg_offset, seg_length FROM entity WHERE id IN (...)`
  2. Cache results in `_segment_locations`
  3. For each entity: `segment_manager.read_record(segment_id, offset, length)` → decode metadata → populate precomp dicts (same logic as before)
  4. **Same-segment fast path**: When populating precomp dicts, if a precomp entry has a non-null 5th element (seg_offset) and the current entity's segment_id matches, we already know the dep's location — use `segment_manager.read_record_at(segment_id, seg_offset)` (reads envelope header to get length, then blob) and add to `_segment_locations`. This means when `load` or `load_flat` later requests that dep, it can skip the SQLite lookup entirely.

  Note: `_load_into_tracked` is already async. The SQLite query goes through the read pool; the `pread` calls are synchronous but fast (like LMDB mmap reads were).

- [x] **4d. Rewrite `load`**

  Current: opens LMDB read txn, reads one blob, decodes CBOR + metadata.

  New: look up `(segment_id, offset, length)` from `_segment_locations` cache (populated by upstream queries, `_load_into_tracked`, or same-segment fast-path); if missing, dispatch a read-pool query on `entity` table. Then `segment_manager.read_record(...)` → decode as before.

- [x] **4e. Rewrite `load_flat`**

  Same pattern as `load`.

- [x] Update LOCAL_CODE_OVERVIEW.md: document new read path

- [x] **4f. Update `test_sqlite_store.py` integration tests**

  - `TestBulkCopyInternals`: updated `bulk_copy_commit` calls to pass `segment_manager`/`segments_dir` instead of `lmdb_env`/`lmdb_data_db`. Reads from segment files instead of LMDB to verify precomp data.
  - All other integration tests pass with the switched read/write paths.
  - Bug fix: raced entities in `bulk_copy_commit` now have their segment locations fetched so `read_blob` can find them.

**Test milestone**: 127 tests pass, 6 fail (2 old LMDB write tests superseded by `test_segment_write.py`, 4 archive export tests that still reference LMDB — Phase 5 work). +27 tests compared to Phase 3.

---

## Phase 5: Infrastructure Cleanup

Export, LMDB removal, and remaining cleanup. (Pool group and setup changes already done in Phase 2f-2g.)

**Files modified**: `sqlite_ops.py`, `sqlite_namespace.py`, `__init__.py`

- [x] **5a. Update `export_archive` in `sqlite_namespace.py`**

  The archive is built by pinning entities into a fresh session (which writes to its own segment files via the normal write path). After the archive session closes:
  - Copy `store.sqlite` to staging
  - Copy `store.sqlite.data2.shards/` directory (all segment files) to staging
  - For tarball: add sqlite + all segment files to tar

  Remove `_compact_copy_lmdb` function. Archive structure changes from `store.sqlite` + `store.sqlite.data1.mdb` to `store.sqlite` + `store.sqlite.data2.shards/seg_*.bin`.

- [x] **5b. Update `_SharedSqlitePoolGroupManager`**: remove LMDB-specific comments. The singleton pattern is still useful for connection pool sharing.

- [x] **5c. Remove runtime LMDB imports**: replace `from fxtr2.data.sqlite_store import lmdb_blob` with `entity_blob` in `sqlite_ops.py` and `sqlite_namespace.py`. The `import lmdb` in `sqlite_ops._setup_database_schema` is removed. `lmdb_blob.py` and `import lmdb` are only used inside `sqlite_migrations.py:migrate_v5_to_v6`.

- [x] **5d. Update error messages**: "not found in LMDB" → "not found in segment store" etc.

- [x] **5e. Update `__init__.py` docstring**: "LMDB-hybrid storage" → "segment-file storage".

- [x] **5f. Delete LMDB files after migration**: Update `migrate_v5_to_v6` to delete the LMDB files (`.data1.mdb` or `.data1.lmdb/` directory) after the migration transaction commits successfully. This is implemented last (rather than in Phase 2) so LMDB files remain available during development and testing of earlier phases.

- [x] **5g. Update `TestArchiveExport` in `test_sqlite_store.py`**

  Assert on `store.sqlite.data2.shards/` contents instead of `store.sqlite.data1.mdb`.

**Test milestone**: 131 pass, 2 fail (old `test_lmdb_write.py` — to be removed in Phase 6). Archive export tests pass.

---

## Phase 6: Remaining Tests — Sealing, CLI, Cleanup

**Files modified**: `test_cli.py`
**Files removed**: `test_lmdb_blob.py`, `test_lmdb_write.py`
**Files modified**: `test_segment_write.py` (add sealing tests)

- [x] **6a. Add sealing mechanism tests to `test_segment_write.py`**

  Make `max_segment_size` configurable in `bulk_copy_commit` (it already has a default of 512 MB from Phase 3). Tests use a small value (e.g. 200 bytes) to trigger rotation without writing megabytes of data.

  Tests:
  - **Batch contiguity**: Write a batch of entities, verify all land in the same segment (same `seg_id` for all entities in the batch).
  - **Rollover trigger**: Write enough batches (with small max size) to trigger segment rotation. Verify the first segment is sealed (`status='sealed'` in `segment_metadata`), a new active segment exists, and the new segment file was created.
  - **Cross-segment reads**: After rollover, load entities from both the old (sealed) and new (active) segments. Verify that an entity in segment 1 whose dep is in segment 0 loads correctly (the precomp entry for that dep should have `seg_offset=null` since it's cross-segment, so the read path does a SQLite lookup).
  - **Same-segment precomp offsets**: Within a single batch, verify that precomp entries for same-batch deps have non-null `seg_offset` values pointing to valid record envelopes.

- [x] **6b. Update `test_git_store/test_cli.py`**: Two locations reference `.data1.mdb`/`.data1.lmdb`:
  - Line ~225: change `.data1.mdb` existence check to `.data2.shards/` directory check
  - Lines ~504-516: update backing store file cleanup to remove `.data2.shards/` directory

- [x] **6c. Remove old test files**: Delete `test_lmdb_blob.py` and `test_lmdb_write.py` (replaced by tests written in Phases 1 and 3).

**Test milestone**: All tests pass, including sealing mechanism tests.

---

## Phase 7: Documentation

- [x] **7a. Update `LOCAL_CODE_OVERVIEW.md`**: Full rewrite of module docs to reflect segment-file architecture. Update file listing, module descriptions, transaction philosophy, crash safety, performance characteristics, design notes, test summary.

- [x] **7b. Update `CODE_OVERVIEW.md`**: Update `sqlite_store/` section — file listing, description, performance notes.

---

## LMDB Reference Removal Summary

| File | Current LMDB usage | Change |
|------|-------------------|--------|
| `lmdb_blob.py` | Core blob module | Keep for migration only; mark deprecated |
| `sqlite_ops.py` | Opens LMDB env, bulk_copy_commit writes | Open segments dir, write to segments |
| `sqlite_namespace.py` | Arena reads from LMDB, export compacts LMDB | Read from segments, export copies segments |
| `sqlite_util.py` | Pool group holds `lmdb_env`/`lmdb_data_db` | Holds `SegmentManager`/`segments_dir` |
| `sqlite_migrations.py` | v3→v4 raises error | Add v5→v6 (reads LMDB, writes segments) |
| `__init__.py` | Docstring mentions LMDB | Update docstring |

After v6, `import lmdb` only appears inside `migrate_v5_to_v6` (and in `lmdb_blob.py` which is only imported by that migration).

## Future Work (Not In Scope)

- **Segment compaction**: rewrite segment files to reclaim space from deleted entities
- **Memory-mapped reads**: replace `pread` with `mmap` for zero-copy reads
- **Entity-level GC**: deleting entities from SQLite does not currently reclaim segment file space
- **Remove `lmdb` dependency**: after sufficient migration period, drop the package entirely
