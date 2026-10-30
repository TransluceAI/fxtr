# sqlite_store Module Overview

This module provides a fast sqlite3-based SQLite store with append-only segment file storage (v6). The key design decisions are:

1. **Segment files for entity data**: Entity blobs (CBOR + metadata) are stored in append-only segment files (`{db_path}.data2.shards/seg_{N:08d}.bin`). SQLite stores metadata plus segment locations (`seg_id`, `seg_offset`, `seg_length` on the entity table).

2. **Thread-safe reads via pread**: Reading entity data uses `os.pread()` on cached file descriptors, which is thread-safe (no shared seek pointer). Multiple reader threads can share the same fd without locking.

3. **Single-transaction write protocol**: Writing entities uses a single IMMEDIATE transaction that allocates IDs on the fly, writes blobs to the active segment, and flushes before SQLite commits (crash-safe).

4. **Direct sqlite3 instead of SQLAlchemy**: Uses Python's built-in sqlite3 module with synchronous API dispatched to a thread pool, avoiding the overhead of SQLAlchemy Core and aiosqlite's async wrapping.

5. **Connection pooling with read/write separation**: Separate pools for readers and writers to take advantage of SQLite's WAL mode, which allows concurrent reads during writes.

6. **Constant SQL strings**: All SQL is defined as module-level constants in `sqlite_ops.py` for better caching and readability.

7. **Small, focused transactions**: Each operation in `sqlite_ops.py` represents one transaction's worth of work. Python logic (deciding what to do next) happens outside transactions.

## File Structure

```
sqlite_store/
├── __init__.py           # Re-exports public classes
├── sqlite_util.py        # ConnectionWorkerPool, ConnectionPoolGroup, transaction helper
├── sqlite_ops.py         # SQL strings and sync database operations
├── sqlite_index_ops.py   # Index-related SQL operations (leaf module, no sqlite_ops imports)
├── sqlite_namespace.py   # Public API classes (SQLiteFxtrSession, etc.)
├── sqlite_migrations.py  # Schema migrations (v2→v3, v3→v4 error, v4→v5, v5→v6)
├── lmdb_blob.py          # LMDB blob encoding/decoding (used only by v5→v6 migration)
├── entity_blob.py        # Entity blob encoding/decoding (LMDB-free, used by segment I/O)
├── segment_io.py         # Segment file I/O (SegmentManager, append, flush, truncate)
└── LOCAL_CODE_OVERVIEW.md
```

## Module Details

### entity_blob.py

Entity blob encoding/decoding for segment-file storage. Pure byte-manipulation with no LMDB dependency. Contains the same blob format as `lmdb_blob.py` (24-byte header + fxtr_id + cbor + metadata sections) plus record envelope helpers for wrapping blobs in segment files.

**Key functions:**
- `encode_blob()`: Encode entity data into blob format
- `decode_blob_header()`, `decode_fxtr_id_bytes()`, `decode_cbor_bytes()`, `decode_metadata()`: Extract data from blobs
- `encode_record_envelope()`: Wrap a blob in `[8-byte LE length][blob]` for segment file storage
- `build_closed_precomp_map_for_segments()`: Build a closed precomp map using a cache + generic `read_blob` callback (replaces `build_closed_precomp_map_cached` from lmdb_blob.py). The `read_blob` callback returns `(bytes, same_segment)` — when `same_segment` is False, all seg_offsets in the decoded precomp map are cleared to None before caching (cross-segment offsets would be stale). This relies on an inductive invariant: every persisted blob's non-None seg_offsets refer to the entity's own segment.

Precomp entries always have 5 elements: `[fxtr_id_bytes, unwraps_to, ctx_node, backstep, seg_offset_or_none]`. The 5th element is the byte offset within the same segment (for fast reads), or `None` for cross-segment deps or the entity's own entry.

### segment_io.py

Low-level I/O for append-only segment files. Segment files live in a shards directory and are named `seg_{N:08d}.bin`.

**`SegmentManager`** class: Holds an LRU cache of open file descriptors for reading segment files.
- `read_record(segment_id, offset, length)`: Read blob at known location (skips envelope header)
- `read_record_at(segment_id, offset)`: Read blob using only offset (reads envelope header to get length)
- `close()`: Close all cached fds

**Module-level helpers:**
- `append_record(fd, blob)`: Write `[8-byte LE len][blob]`, returns `(offset, length)`
- `flush_segment(fd)`: Platform-aware durable flush (`fdatasync` on Linux, `F_FULLFSYNC` on macOS)
- `truncate_to_cursor(path, write_cursor)`: Crash recovery — truncate file to committed position

### lmdb_blob.py

LMDB blob format for entity storage. Used only by the v5→v6 migration; new code uses `entity_blob.py` instead.

**Blob Format:**

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

**Metadata Object:**

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

The precomp map is **closed**: it contains an entry for the entity itself, every entity in its deps list, and every entity that appears as a value in any precomp tuple. This means reading a single LMDB blob provides everything needed to populate the arena's precomp dicts without any SQLite queries.

**Key functions:**
- `encode_blob()`: Encode entity data into LMDB blob format
- `decode_blob_header()`, `decode_fxtr_id_bytes()`, `decode_cbor_bytes()`, `decode_metadata()`: Extract data from blobs
- `build_closed_precomp_map()`: Build a closed precomp map by reading deps' LMDB blobs and merging
- `build_closed_precomp_map_cached()`: Like above but uses an in-memory cache and an existing LMDB write txn (for bulk copy)

### sqlite_util.py

Connection pool infrastructure for managing sqlite3 connections.

**`ConnectionWorkerPool`** (primary, used by `sqlite_namespace.py`)

Persistent worker threads that each hold a DB connection for their lifetime:

- Work items are submitted to a shared `queue.Queue` and picked up by the next available worker
- Each worker creates a connection on startup and holds it until shutdown
- Uses `anyio.CapacityLimiter` to bound actual thread concurrency to `max_connections`
- Supports per-connection setup/teardown callbacks
- Uncommitted transactions are rolled back after each work item
- Workers are cancelled via the cancel scope returned by `start_all_workers()`

Key methods:
```python
# Start all workers, get cancel scope for shutdown
cancel_scope = await tg.start(pool.start_all_workers)

# Submit work (same signature as ConnectionPool)
result = await pool.run_sync_with_connection(fn, arg1, arg2)

# Shut down: cancel workers, then close pool
cancel_scope.cancel()
await pool.aclose()
```

Uses `abandon_on_cancel=True` so that if the async side is cancelled, the thread is abandoned. Workers detect cancellation via `anyio.from_thread.check_cancelled()`.

**`ConnectionWorkerPoolGroup`**

A dataclass holding paired read/write worker pools plus segment file manager:

```python
@dataclasses.dataclass
class ConnectionWorkerPoolGroup:
    write_pool: ConnectionWorkerPool | None  # None in read-only mode; typically max_connections=1
    read_pool: ConnectionWorkerPool          # Typically max_connections=5
    segment_manager: SegmentManager          # For reading entity data from segment files
    segments_dir: str                        # Path to the segment files directory
```

Should be created via `setup_database_and_create_worker_pool_group()` (read-write) or `setup_readonly_pool_group()` (read-only) in `sqlite_ops.py`.

**`ConnectionPool`** (original, kept for flexibility)

A pool of sqlite3 connections with capacity limiting and per-call thread dispatch:

- Creates connections lazily and caches them in a freelist
- Uses `anyio.CapacityLimiter` to bound concurrent connections
- Dispatches each call via `anyio.to_thread.run_sync()`
- No longer used by `sqlite_namespace.py` but kept for other callers

**`ConnectionPoolGroup`**: Paired read/write `ConnectionPool` instances (legacy, no longer used by `sqlite_namespace.py`).

### sqlite_ops.py

Contains all SQL strings and sync database operations:

- **SQL strings as module-level constants**: All SQL is defined at module level (e.g., `SQL_INSERT_SESSION`, `SQL_MARK_SESSION_GARBAGE`) for better SQLite caching and readability.

- **Sync functions**: Each function takes a `sqlite3.Connection` as the first parameter and performs focused database operations. Functions manage their own transactions using `with transaction(conn, mode):`.

- **Schema version 6** (current): Replaces LMDB with append-only segment files. New `segment_metadata` table, `seg_id/seg_offset/seg_length` columns on entity table, `entity_reservations` table dropped. Entity data stored in `{db_path}.data2.shards/seg_{N:08d}.bin` files. `storage_location` is `'data2/seg'` for all entities.

- **Schema version 5**: Adds `ns_index_registry` table for namespace indexing.

- **Schema version 4**: Uses LMDB for entity data storage (`storage_location='data1/1'`). Superseded by v6.

- **Segment file layout**: Each SQLite database at `{path}` gets a companion directory at `{path}.data2.shards/` containing segment files named `seg_{N:08d}.bin`. The `segment_metadata` table tracks each segment's status ('active' or 'sealed') and committed write cursor.

- **Operation categories**:
  - Session operations: `insert_session`, `mark_session_garbage`, `find_and_delete_orphan_garbage_session`
  - Namespace operations: `get_or_create_user_namespace`, `get_user_namespace` (read-only lookup)
  - Pin operations: `create_or_update_pin`, `unpin_and_maybe_rebind`, `list_pins_with_prefix`
  - Arena operations: `create_arena`, `create_arena_with_entities`, `reassign_arena_to_garbage_session`
  - Read-only arena ops: `retrieve_pinned_entity`, `lookup_entity_in_namespace`
  - Garbage collection: `check_and_claim_gc`, `release_gc_claim`, `run_one_gc_step`
  - Backref queries: `get_backrefs_for_entity`
  - Bulk copy operations: `discover_entity_by_public_id`, `bulk_copy_commit`, `bulk_copy_commit_preexisting_only`

- **Bulk copy protocol** (single-transaction, segment-file based):
  1. **Discover** (read-only): Walk entity graph via read pool. Entities in entity table go to preexisting set; others are loaded from source and added to a topologically-sorted save queue.
  2. **Big commit**: Single IMMEDIATE txn with `flock(LOCK_EX)` on `write.lock`:
     - Creates the arena, reads active segment info.
     - Pre-fetches segment locations for all preexisting deps (batch SELECT).
     - Walks save queue in topological order. For each entity: race-check, INSERT entity row (allocate ID on the fly), build precomp (cached, reads via `read_blob` callback that returns `(bytes, same_segment)` — cross-segment inherited offsets are cleared at cache time), encode blob, fill in same-batch offsets, `append_record` to segment file, UPDATE entity with seg location, insert entity_dep rows.
     - `flush_segment` (fdatasync) before SQLite commits — crash-safe.
     - Updates `segment_metadata.write_cursor`. Rotates segment if over 512 MB.
     - Recursive CTE for namespace/arena membership. SQLite COMMIT.
     - Releases flock.

- **Database setup**:
  - `setup_database_and_create_worker_pool_group()` (and async variant): Initializes the database schema, creates the segments directory, performs crash recovery (truncate active segment to committed write_cursor), and returns a `ConnectionWorkerPoolGroup` with a `SegmentManager`.
  - `setup_readonly_pool_group()` (and async variant): Opens an existing database in read-only mode (`mode=ro`). Validates the database exists and has the correct schema version. Returns a `ConnectionWorkerPoolGroup` with `write_pool=None`. No migrations, no session creation, no segment truncation.

### sqlite_index_ops.py

Leaf module containing all index-related SQL helpers for namespace indexing. Does NOT import from `sqlite_ops.py` (dependency direction: `sqlite_ops` imports from `sqlite_index_ops`).

- **Write-pool functions**: `register_indexer_start`, `write_index_entries_batch`, `try_finish_registration`, `set_registration_garbage`
- **Read-pool functions**: `get_indexed_registrations_for_prefix`, `get_unprocessed_pins`, `get_indexed_registrations_for_namespace`, `resolve_indexer_for_query`, `execute_index_query`
- **GC/session-death helpers**: `mark_building_registrations_garbage_for_session`, `gc_cleanup_one_garbage_registration`
- **Private helpers** (called within transactions from `sqlite_ops.py`): `_ensure_index_tables_exist`, `_write_index_rows`, `_write_processed_marker`, `_delete_index_entries_for_entity`, `_drop_index_tables_if_unused`, `_verify_indexed_registrations`
- **`IndexMismatchError`**: Raised when CAS check fails during `create_or_update_pin`

### sqlite_namespace.py

Public API classes that provide the entity store interface:

- **`SQLiteFxtrSession`**: Session management with lifecycle and garbage collection
  - Creates and manages connection pools via `ConnectionWorkerPoolGroup`
  - Starts worker threads via `start_all_workers()` and cancels them on session close
  - Handles session lifecycle (start, close, garbage worker)
  - Manages crash recovery via session lock files
  - Only supports file-based databases (no in-memory DBs in v4)
  - **Read-only mode** (`read_only=True`):
    - No session row inserted in database, no session lock file created
    - No garbage collection worker started
    - SQLite connections use `mode=ro`, `write_pool` is `None`
    - Read operations work normally (retrieve, list_pins, iter_matches, sql, try_lookup, list_indexers)
    - Write operations (pin, unpin, register_indexer, unregister_indexer, export_archive) raise `RuntimeError("Store is open in read-only mode")`
    - Invisible to other processes; safe on network filesystems with insufficient locking

- **`SQLiteNamespaceBackend`**: Namespace operations implementing `IndexablePinningBackend` and `DeepFetchable[B]`. Backend methods do **not** take a backend lease (draining is managed via session state); cross-arena methods still take a source-arena lease. Callers get a `Lease[B]` for returned arenas via `backend.get_lease()`.
  - Named pins: `pin_entity_from_arena(key, entity_id, source, src_lease, *, overwrite)`, `unpin_entity(key)`, `retrieve_pinned(key)`, `list_pins(prefix)`
  - Indexing: `register_indexer(indexer_id, source, src_lease, key_prefix)`, `unregister_indexer(indexer_id, key_prefix)`, `list_indexers()`, `iter_matches(...)`
  - Deep fetch: `try_lookup(node_id)` returns a `ReadableArena[B]` for pinned entities (no `backrefs` API; SQLite still tracks reverse deps internally for GC via `get_backrefs_for_entity` in `sqlite_ops`)
  - `retrieve_pinned()` and `try_lookup()` use read pool for lightweight arena creation
  - `pin_entity_from_arena()` uses a CAS loop to atomically pin + write index data
  - Caches an `IndexComputer` instance for reuse across indexing operations
  - `_build_tmp_arena_copying_from_external_arena()`: Two-phase batched copy (discover sweep → big commit). Uses `_discover_sweep_for_copy()` for read-only discovery. IDs allocated on the fly within the single-transaction big commit.
  - `export_archive(path, tar=True, gz=False)`: Export namespace to archive (tarball or raw sqlite+segments). Copies the SQLite database and the `{db_path}.data2.shards/` directory.

- **`SQLiteNamespaceArena`**: Lightweight, read-only arena implementing `ReadableArena[B]`. Lease-gated: every public method validates the caller's `Lease[B]` via `_with_validated_arena_lease` (the same-named helper at the top of `sqlite_namespace.py` — it applies only to the arena, not to the backend).
  - Entity tracking: `is_tracked()`, `load()`, `load_flat()`, `abandon()`, `copy_to_new_arena()`
  - Not associated with DB state — pure Python-side tracking
  - **Segment location cache**: `_segment_locations: dict[int, tuple[int, int, int | None]]` maps internal_id to (seg_id, seg_offset, seg_length). `seg_length=None` means length unknown (from same-segment fast path). Pre-populated from upstream queries (`retrieve_pinned_entity`, `lookup_entity_in_namespace`) and batch-fetched via `fetch_entity_segment_locations` when needed.
  - **Synchronous segment reads**: All entity data reads use `segment_manager.read_record()` (pread), no thread dispatch
    - `_read_entity_blob()`: Helper that handles both full locations and partial locations (length=None from same-segment fast path, uses `read_record_at`)
    - `_process_precomp_entries()`: Shared helper that registers precomp entries — maps IDs, populates precomp dicts, caches same-segment dep locations for the fast read path
    - `_load_dn_impl()`: Shared d/n blob loading used by both `load()` and `load_flat()` — fetches segment location, reads blob, decodes metadata, calls `_process_precomp_entries()`
    - `load()`: Calls `_load_dn_impl()`, tracks direct deps, deserializes CBOR
    - `load_flat()`: Calls `_load_dn_impl()` for d/n nodes (or constructs from precomp dicts for derived nodes), tracks all deps via `_load_into_tracked()`
    - `_load_into_tracked()`: Batch-fetches segment locations, reads blobs, populates precomp dicts via `_process_precomp_entries()`. Same-segment fast path: when a precomp entry has a non-null 5th element, caches the dep's location without a SQLite lookup.
  - Precomputed data (unwrap targets, context nodes, etc.) extracted from segment blob metadata via `entity_blob.decode_metadata()`

### sqlite_migrations.py

Database schema migrations for upgrading SQLite databases between versions:

- **Version tracking**: Uses SQLite's `PRAGMA user_version`
- **Automatic migration**: `run_migrations()` runs migrations when needed
- **v2→v3 migration**: Adds performance indices for GC and orphan detection
- **v3→v4 migration**: Raises error with manual migration instructions (v4 uses LMDB, no automatic migration path)
- **v4→v5 migration**: Adds `ns_index_registry` table
- **v5→v6 migration**: Migrates entity data from LMDB to segment files. Creates `segment_metadata` table, adds `seg_id/seg_offset/seg_length` columns to entity table, drops `entity_reservations`, reads all LMDB blobs and writes them to a segment file with updated precomp entries (5th element for same-segment offsets). LMDB files (`.data1.lmdb` directory and `.data1.mdb` file + lock) are deleted after migration completes successfully.

## Transaction Philosophy

Transactions should be kept as small and granular as possible while preserving invariants:

1. Each `sqlite_ops` function manages its own transaction(s) using `with transaction(conn, ...)`
2. Python work (deciding what to enqueue, processing results) happens outside transactions
3. The async caller does: `await pool.run_sync(txn1)` → Python logic → `await pool.run_sync(txn2)` → ...

This pattern minimizes lock contention and allows cancellation to happen between transactions.

## Cancellation Handling

Before committing any write transaction that might be long-running, sync functions should call `check_cancelled`, e.g.
```python
from sqlite_util import transaction

def some_sync_op(conn: sqlite3.Connection, ...):
  with transaction(conn, "IMMEDIATE"):  # starts a transaction
      # ... do writes, etc, get result

      # then, right before finishing the transaction:
      anyio.from_thread.check_cancelled()
      # (if the run_sync call was cancelled, this will abort the transaction)

  return result
```

This raises an exception if the async task was cancelled while waiting, causing rollback instead of commit.

## Testing

Tests are in `tests/test_sqlite_store/` (127 tests) and `tests/test_store_indexing/test_backend_integration.py` (20 tests):

- **`test_sqlite_util.py`** (10 tests): Connection pool mechanics
  - Basic operations (run sync functions, get results)
  - Connection reuse via freelist
  - Capacity limiting
  - Automatic rollback of uncommitted transactions
  - Readonly pool enforcement
  - Cancellation behavior
  - Worker pool: basic ops, concurrency, connection reuse, setup callbacks, exception propagation, rollback, close/drain, cancelled flag

- **`test_sqlite_store.py`** (71 tests): End-to-end functionality
  - Database initialization and setup
  - Session lifecycle management
  - Namespace operations and pinning
  - Arena operations (load, track, abandon)
  - Cross-namespace copying
  - Garbage collection
  - Session file crash recovery
  - Segment file read/write integration (bulk copy precomp cache, race handling)

- **`test_sqlite_migrations.py`** (9 tests): Schema migrations
  - v2→v3 migration
  - v3→v4 migration error
  - Fresh database initialization
  - Version validation
  - v5→v6 migration (with entities, without entities, entity_reservations drop)

- **`test_entity_blob.py`** (15 tests): Entity blob encoding/decoding and precomp map building
  - Encode/decode roundtrip with and without CBOR
  - Record envelope format
  - 5th precomp element support
  - `build_closed_precomp_map_for_segments`: no deps, with deps, deduplication, missing dep error, cache usage, cross-segment offset clearing, same-segment offset preservation

- **`test_segment_io.py`** (15 tests): Segment file I/O
  - Record append/read, FD cache eviction, crash recovery

- **`test_segment_write.py`** (10 tests): Segment file write operations via `bulk_copy_commit`
  - `TestSegmentWrite` (5 tests): Write blob and verify segment file contents, derived node empty CBOR, same-segment precomp offsets, write.lock creation, write_cursor update
  - `TestSegmentSealing` (5 tests): Batch contiguity, rollover trigger (segment rotation), cross-segment reads (dep in sealed segment), same-segment precomp offset valid envelope verification, inherited precomp offset cleared cross-segment

## Performance Characteristics

Key optimizations:

1. **Thread-safe pread reads**: `os.pread()` on cached fds — no seek pointer contention, no thread dispatch overhead
2. **Closed precomp maps**: Single blob read provides all metadata needed for an entity and its dependencies
3. **Same-segment fast path**: Precomp entries with a 5th element (seg_offset) allow skipping SQLite lookups for deps in the same segment. Cross-segment inherited offsets are cleared to None at write time (in `build_closed_precomp_map_for_segments`) to maintain the invariant that non-None offsets always refer to the entity's own segment.
4. **Direct sqlite3 access**: Avoids SQLAlchemy Core overhead
5. **Constant SQL strings**: SQLite can cache prepared statements
6. **Connection pooling**: Separate read/write pools for WAL mode concurrency
7. **Single-transaction copy**: Entity copy uses 1-2 total transactions, with IDs allocated on the fly

## Design Notes

- **entity_dep is kept**: The bulk copy CTE walks `entity_dep` to find transitive deps for namespace membership, GC cascades through `entity_dep`, and backref queries use it.

- **Precomp map size**: Provenance chains are typically length 0 or 1, so the closed precomp map is small. The overhead is acceptable for the performance gains.

- **Segment data outlives namespaces**: Entity data in segment files is never cleaned up by current namespace-level GC. This is acceptable. Future entity-level GC should clean up both SQLite entity rows and reclaim segment space.

- **Crash safety invariant**: Segment data is flushed (`fdatasync`/`F_FULLFSYNC`) before SQLite commits. On crash recovery, startup truncates the active segment to the committed `write_cursor`. Therefore: **all segment data referenced by SQLite is valid; trailing unflushed data is truncated**.

- **Write locking**: `flock(LOCK_EX)` on `{segments_dir}/write.lock` serializes concurrent writers. The lock is held for the duration of the segment write + SQLite transaction. Uses `fcntl.flock` which is Unix-only (Linux/macOS); would need a platform abstraction if Windows support were ever needed.

- **Segment rotation**: When the active segment exceeds 512 MB (configurable), it's sealed and a new active segment is created.

- **No in-memory databases**: Segment files require a filesystem path. Tests use temporary directories via `tmp_path` fixture.
