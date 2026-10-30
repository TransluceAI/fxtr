# sqlite_store Module Overview

This module provides a fast sqlite3-based replacement for `sqlite_arena.py`. The key design decisions are:

1. **Direct sqlite3 instead of SQLAlchemy**: Uses Python's built-in sqlite3 module with synchronous API dispatched to a thread pool, avoiding the overhead of SQLAlchemy Core and aiosqlite's async wrapping.

2. **Connection pooling with read/write separation**: Separate pools for readers and writers to take advantage of SQLite's WAL mode, which allows concurrent reads during writes.

3. **Constant SQL strings**: All SQL is defined as module-level constants in `sqlite_ops.py` for better caching and readability.

4. **Small, focused transactions**: Each operation in `sqlite_ops.py` represents one transaction's worth of work. Python logic (deciding what to do next) happens outside transactions.

## File Structure

```
sqlite_store/
├── __init__.py           # Re-exports public classes
├── sqlite_util.py        # ConnectionPool, ConnectionPoolGroup, transaction helper
├── sqlite_ops.py         # SQL strings and sync database operations
├── sqlite_namespace.py   # Public API classes (SQLiteFxtrSession, etc.)
└── LOCAL_CODE_OVERVIEW.md
```

## Module Details

### sqlite_util.py

Connection pool infrastructure for managing sqlite3 connections.

**`ConnectionPool`**

A pool of sqlite3 connections with capacity limiting:

- Creates connections lazily and caches them in a freelist
- Uses `anyio.CapacityLimiter` to bound concurrent connections
- Supports per-connection setup callback (e.g., `PRAGMA foreign_keys = ON`)
- Automatically rolls back uncommitted transactions when connections are returned
- Supports readonly mode via `SQLITE_OPEN_READONLY` flag

Key method:
```python
async def run_sync_with_connection(
    self,
    fn: Callable[Concatenate[sqlite3.Connection, P], R],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
```

This dispatches a sync function to a thread pool, providing it with a pooled connection. The connection is automatically returned to the pool after the function completes.

Uses `abandon_on_cancel=True` so that if the async side is cancelled, the thread is abandoned (rather than waiting for it). The thread can detect this via `anyio.from_thread.check_cancelled()` and bail out before committing.

**`ConnectionPoolGroup`**

A dataclass holding paired read/write pools:

```python
@dataclasses.dataclass
class ConnectionPoolGroup:
    write_pool: ConnectionPool  # Typically max_connections=1
    read_pool: ConnectionPool   # Typically max_connections=N (reads go through this pool by convention)
```

Should be created via `setup_database_and_create_pool_group()` in `sqlite_ops.py`, which handles schema initialization before creating the pools.

### sqlite_ops.py

Contains all SQL strings and sync database operations:

- **SQL strings as module-level constants**: All SQL is defined at module level (e.g., `SQL_INSERT_SESSION`, `SQL_MARK_SESSION_GARBAGE`) for better SQLite caching and readability.

- **Sync functions**: Each function takes a `sqlite3.Connection` as the first parameter and performs focused database operations. Functions manage their own transactions using `with transaction(conn, mode):`.

- **Operation categories**:
  - Session operations: `insert_session`, `mark_session_garbage`, `find_and_delete_orphan_garbage_session`
  - Namespace operations: `get_or_create_user_namespace`
  - Pin operations: `create_or_update_pin`, `unpin_and_maybe_rebind`, `list_pins_with_prefix`
  - Arena operations: `create_arena`, `create_arena_with_entities`, `verify_in_namespace_and_add_to_arena`, `reassign_arena_to_garbage_session`
  - Garbage collection: `check_and_claim_gc`, `release_gc_claim`, `run_one_gc_step`
  - Precomputed data: `load_precomp_data_for_entities`
  - Backref queries: `get_backrefs_for_entity`
  - Entity operations: `load_and_add_children_to_arena`
  - Copy operations: `copy_discover`, `copy_save`, `copy_track_deps`, `copy_track`

- **Database setup**: `setup_database_and_create_pool_group()` initializes the database schema and returns a `ConnectionPoolGroup`.

### sqlite_namespace.py

Public API classes that mirror `sqlite_arena.py` interface:

- **`SQLiteFxtrSession`**: Session management with lifecycle and garbage collection
  - Creates and manages connection pools via `ConnectionPoolGroup`
  - Handles session lifecycle (start, close, garbage worker)
  - Manages crash recovery via session lock files
  - Supports both file-based and in-memory databases

- **`SQLiteNamespaceBackend`**: Namespace operations implementing `PersistentPinningBackend` and `ObservableStore`
  - Named pins: `pin_entity_from_arena()`, `unpin_entity()`, `retrieve_pinned()`, `list_pins()`
  - Backrefs: `backrefs()` for querying entity relationships
  - Dispatches to `sqlite_ops` functions via `pool_group.write_pool.run_sync_with_connection()`

- **`SQLiteNamespaceArena`**: Arena operations implementing `CopyTargetArena`
  - Entity tracking: `is_tracked()`, `load()`, `abandon()`
  - Copy operations: `copy_from_arena()`, `copy_to_new_arena()`
  - Pending-task machinery for asynchronous copy operations
  - Dispatches to `sqlite_ops` functions via connection pools

## Transaction Philosophy

Transactions should be kept as small and granular as possible while preserving invariants:

1. Each `sqlite_ops` function manages its own transaction(s) using `with transaction(conn, ...):`
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

Tests are in `tests/test_sqlite_store/` (87 tests total):

- **`test_sqlite_util.py`** (13 tests): Connection pool mechanics
  - Basic operations (run sync functions, get results)
  - Connection reuse via freelist
  - Capacity limiting
  - Automatic rollback of uncommitted transactions
  - Readonly pool enforcement
  - Cancellation behavior

- **`test_sqlite_store.py`** (74 tests): End-to-end functionality
  - Database initialization and setup
  - Session lifecycle management
  - Namespace operations and pinning
  - Arena operations (load, track, abandon)
  - Cross-namespace copying
  - Garbage collection
  - Session file crash recovery
  - Memory database support

All tests pass. The implementation is feature-complete and provides the same interface as `sqlite_arena.py` with significant performance improvements.

## Implementation Status

See `plans/phase2/stage8_sqlite_speedup.md` for the full implementation plan.

**Status: Complete ✅**

- [x] Phase 1: Core Infrastructure (`sqlite_util.py`)
- [x] Phase 2: Bootstrap with Copied Code
- [x] Phase 3: Setup Infrastructure
- [x] Phase 4: Incremental Op Migration (all operations migrated to sqlite3)
- [x] Phase 5: Cleanup (SQLAlchemy removed, memory DB support restored)
- [x] Phase 6: Documentation (CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md updated)

## Performance Characteristics

Key optimizations compared to `sqlite_arena.py`:

1. **Direct sqlite3 access**: Avoids SQLAlchemy Core overhead and aiosqlite's async wrapping
2. **Constant SQL strings**: SQLite can cache prepared statements
3. **Connection pooling**: Separate read/write pools for WAL mode concurrency
4. **Sync operations**: Database I/O runs in thread pool, avoiding async overhead for blocking operations
5. **Small transactions**: Minimizes lock contention and allows better concurrency
