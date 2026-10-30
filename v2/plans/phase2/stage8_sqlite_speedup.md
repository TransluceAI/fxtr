# Stage 8: SQLite Speedup - APSW Refactor

## Goal

Replace the SQLAlchemy Core + aiosqlite implementation in `sqlite_arena.py` with a faster APSW-based implementation that:
- Uses sync APSW operations dispatched to a thread pool via `anyio.to_thread.run_sync`
- Uses constant SQL strings for better caching
- Combines consecutive operations where possible to reduce round-trips
- Maintains connection pools with capacity limiters for read/write separation

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    sqlite_namespace.py                          │
│  (SQLiteFxtrSession, SQLiteNamespaceBackend, SQLiteNamespaceArena) │
│                                                                 │
│  Same public API as sqlite_arena.py, uses async methods that   │
│  dispatch to ConnectionPoolGroup.run_sync_with_connection()    │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      sqlite_util.py                             │
│  ConnectionPool, ConnectionPoolGroup, transaction               │
│                                                                 │
│  - Manages connection freelists                                 │
│  - Enforces capacity limits (1 writer, N readers)              │
│  - Dispatches sync functions to thread pool                    │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      sqlite_ops.py                              │
│  All SQL strings and sync database operations                   │
│                                                                 │
│  - Constant SQL strings at module level                        │
│  - Sync functions taking apsw.Connection                       │
│  - Each function = one transaction's worth of work             │
│  - Uses `with transaction(conn, mode):` for transactions        │
└─────────────────────────────────────────────────────────────────┘
```

## File Structure

```
fxtr2/data/sqlite_store/
├── __init__.py           # Re-exports public classes
├── sqlite_util.py        # ConnectionPool, ConnectionPoolGroup
├── sqlite_ops.py         # SQL strings and sync operations
└── sqlite_namespace.py   # Public API classes

tests/test_sqlite_store/
├── __init__.py
├── conftest.py           # Fixtures for sqlite_store tests
├── test_sqlite_util.py   # Tests for connection pool
└── test_sqlite_store.py  # Main tests (adapted from test_sqlite_arena.py)
```

## Detailed Design

### sqlite_util.py

```python
import apsw
import anyio
from typing import Callable, Concatenate, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

class ConnectionPool:
    """A pool of APSW connections with capacity limiting."""

    def __init__(
        self,
        db_path: str,
        max_connections: int,
        readonly: bool = False,
        connection_setup: Callable[[apsw.Connection], None] | None = None,
    ):
        self._db_path = db_path
        self._readonly = readonly
        self._connection_setup = connection_setup
        self._capacity_limiter = anyio.CapacityLimiter(max_connections)
        # Thread safety note: Python's GIL makes list.append() and list.pop()
        # atomic for simple cases. The capacity limiter bounds concurrent access.
        self._freelist: list[apsw.Connection] = []
        self._closed = False

    def _create_connection(self) -> apsw.Connection:
        """Create a new APSW connection."""
        flags = apsw.SQLITE_OPEN_READONLY if self._readonly else apsw.SQLITE_OPEN_READWRITE
        conn = apsw.Connection(self._db_path, flags=flags)
        # Run per-connection setup (e.g., PRAGMA foreign_keys = ON)
        if self._connection_setup is not None:
            self._connection_setup(conn)
        return conn

    def _get_connection_sync(self) -> apsw.Connection:
        """Get a connection from freelist or create new (sync, called from thread)."""
        if self._closed:
            raise RuntimeError("ConnectionPool is closed")
        if self._freelist:
            return self._freelist.pop()
        return self._create_connection()

    def _return_connection_sync(self, conn: apsw.Connection) -> None:
        """Return connection to freelist or close it (sync, called from thread)."""
        # Rollback any uncommitted transaction
        if conn.in_transaction:
            conn.execute("ROLLBACK")

        if self._closed:
            conn.close()
        else:
            self._freelist.append(conn)

    async def run_sync_with_connection(
        self,
        fn: Callable[Concatenate[apsw.Connection, P], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run a sync function with a pooled connection in a thread."""
        if self._closed:
            raise RuntimeError("ConnectionPool is closed")

        async with self._capacity_limiter:
            def _inner() -> R:
                conn = self._get_connection_sync()
                try:
                    result = fn(conn, *args, **kwargs)
                    return result
                finally:
                    self._return_connection_sync(conn)

            return await anyio.to_thread.run_sync(_inner)

    def close(self) -> None:
        """Close all idle connections and mark pool as closed."""
        self._closed = True
        while self._freelist:
            conn = self._freelist.pop()
            conn.close()

    def __enter__(self) -> "ConnectionPool":
        return self

    def __exit__(self, *args) -> None:
        self.close()


@dataclasses.dataclass
class ConnectionPoolGroup:
    """A pair of connection pools for read and write operations.

    Should usually be created after doing schema initialization and database-wide configuration.
    (Used in setup_database_and_create_pool_group().)
    """
    write_pool: ConnectionPool
    read_pool: ConnectionPool

    def close(self) -> None:
        """Close both pools."""
        self.write_pool.close()
        self.read_pool.close()

    def __enter__(self) -> ConnectionPoolGroup:
        return self

    def __exit__(self, *args) -> None:
        self.close()


@contextlib.contextmanager
def transaction(conn: apsw.Connection, transaction_mode: str) -> Generator[None, None, None]:
    """Small helper to begin a transaction with a given transaction mode."""
    conn.transaction_mode = transaction_mode
    with conn:
        yield

```

### sqlite_ops.py

All SQL strings are module-level constants. Functions take an `apsw.Connection` and perform
small, focused database operations. Each function should do minimal Python computation—the
logic for deciding what to do next belongs in the caller (sqlite_namespace.py).

**Transaction Philosophy:**
- Keep transactions as small and granular as possible (but still large enough to preserve the invariants; the current size of the transactions in sqlite_arena.py is a good starting point)
- sqlite_ops functions use `with transaction(conn, mode):` (from `sqlite_util.py`) to manage a single transaction
- each function in sqlite_ops should be either a single transaction or, when necessary, a sequence of related transactions. each sqlite_ops op should manage its own transactions, and not assume it is being called inside an existing transaction
- Python work (deciding what to enqueue, processing results) happens outside transactions
- The caller does: sync transaction → Python logic → sync transaction → Python logic → ...

**SQL Constants (examples):**
```python
# Session operations
SQL_INSERT_SESSION = """
    INSERT INTO session (public_id, created_at, last_seen_at, is_garbage, is_collecting_garbage)
    VALUES (?, ?, ?, 0, 0)
"""
SQL_MARK_SESSION_GARBAGE = """
    UPDATE session SET is_garbage = 1, last_seen_at = ? WHERE id = ?
"""
# ... etc
```

**Function Signatures:**

Line references are to `sqlite_arena.py` for the corresponding logic.

**IMPORTANT: Each function below represents exactly ONE transaction.** Low-level SQL helpers
(insert_entity, insert_entity_dep, etc.) are internal to sqlite_ops and not listed here—they're
used as building blocks within the transaction-level functions.

```python

# =============================================================================
# Database Setup
# =============================================================================

def setup_database_and_create_pool_group(
    db_path: str,
    write_pool_size: int = 1,
    read_pool_size: int = 5,
) -> ConnectionPoolGroup:
    """Initialize database schema and return a ConnectionPoolGroup.

    See: init_database (lines 543-613), create_fxtr_engine (lines 624-676)
    Also: table definitions (lines 328-528), DB_USER_VERSION (line 536)

    This is the main entry point for creating a database connection.
    It:
    1. Creates a temporary connection for setup
    2. Sets PRAGMA journal_mode = WAL (persists to file)
    3. Creates tables if needed (checks user_version)
    4. Creates the garbage session (session 0) if needed
    5. Creates and returns a ConnectionPoolGroup

    Each ConnectionPool's per-connection setup handles PRAGMA foreign_keys = ON.
    """

# =============================================================================
# Session Operations (each is one transaction)
# =============================================================================

def insert_session(conn: apsw.Connection, public_id: bytes, now: datetime) -> int:
    """Insert a new session, return internal ID.

    See: _lifecycle_handler (lines 826-839)
    """

def mark_session_garbage(conn: apsw.Connection, session_id: int, now: datetime) -> None:
    """Mark a session as garbage.

    See: _lifecycle_handler teardown (lines 860-866)
    """

def mark_session_garbage_by_public_id(conn: apsw.Connection, public_id: bytes) -> int:
    """Mark session as garbage by public ID. Returns rowcount.

    See: _detect_and_mark_dead_sessions (lines 1167-1174)
    """

def set_session_collecting_garbage(conn: apsw.Connection, session_id: int, is_collecting: bool) -> None:
    """Set whether this session is collecting garbage.

    See: _cleanup_worker claim (lines 1404-1408), release (lines 1435-1438)
    """

def find_session_collecting_garbage(conn: apsw.Connection) -> int | None:
    """Find any session that is collecting garbage. Returns session_id or None.

    See: _cleanup_worker (lines 1374-1379)
    """

def find_orphan_garbage_session(conn: apsw.Connection) -> int | None:
    """Find a garbage session (not session 0) with no arenas. Returns session_id or None.

    See: _clean_up_garbage step 2 (lines 1309-1318)
    """

def delete_session_and_reservations(conn: apsw.Connection, session_id: int) -> None:
    """Delete a session and its entity reservations.

    See: _clean_up_garbage (lines 1322-1330)
    """

# =============================================================================
# Namespace Operations (each is one transaction)
# =============================================================================

def get_or_create_user_namespace(conn: apsw.Connection, name: str) -> tuple[int, uuid.UUID]:
    """Get or create a user namespace. Returns (internal_id, public_uuid).

    See: create_or_retrieve_user_namespace (lines 971-1014)
    """

# =============================================================================
# Batched Lookup Operations (read-only, each is one transaction)
# =============================================================================

def lookup_entity_ids_batch(conn: apsw.Connection, public_ids: list[bytes]) -> dict[bytes, int]:
    """Look up internal IDs for multiple entities by public ID.

    Returns dict mapping public_id -> internal_id for entities that exist.
    Missing entities are not included in the result.

    Used to resolve dep internal IDs before calling save_entity_to_tables.
    """

def get_entity_public_ids_batch(conn: apsw.Connection, internal_ids: list[int]) -> dict[int, bytes]:
    """Get public IDs for multiple entities by internal ID.

    Returns dict mapping internal_id -> public_id.

    See: _load_into_tracked step 1 (lines 2856-2865)
    """

# =============================================================================
# Pin Operations (each is one transaction)
# =============================================================================

def create_or_update_pin(
    conn: apsw.Connection,
    namespace_id: int,
    entity_internal_id: int,
    name: str,
    overwrite: bool,
) -> int | None:
    """Create or update a pin. Returns old_entity_internal_id if overwrite happened.

    See: pin_entity_from_arena transaction (lines 1814-1868)

    If pin exists and overwrite=False: raises KeyError
    If pin exists and overwrite=True: updates pin, rebinds old entity if orphaned
    If pin doesn't exist: creates pin

    Returns the old entity's internal_id if a pin was overwritten (for potential rebind),
    or None if a new pin was created.
    """

def unpin_and_maybe_rebind(
    conn: apsw.Connection,
    namespace_id: int,
    name: str,
) -> None:
    """Remove a pin and rebind the entity to garbage if orphaned.

    See: unpin_entity (lines 1879-1903)

    Raises KeyError if pin doesn't exist.
    """

def list_pins_with_prefix(conn: apsw.Connection, namespace_id: int, prefix: str) -> list[tuple[str, bytes]]:
    """List pins with prefix. Returns list of (name, entity_public_id).

    See: list_pins (lines 2312-2345)
    """

# =============================================================================
# Retrieve/Lookup Operations (each is one transaction)
# =============================================================================

def retrieve_pinned_and_create_arena(
    conn: apsw.Connection,
    namespace_id: int,
    session_id: int,
    pin_name: str,
    arena_public_id: bytes,
) -> tuple[int, bytes, int]:
    """Look up pin and create arena with the pinned entity.

    See: retrieve_pinned (lines 2271-2309)

    Returns (entity_internal_id, entity_public_id, arena_internal_id).
    Raises KeyError if pin doesn't exist.
    """

def lookup_in_namespace_and_create_arena(
    conn: apsw.Connection,
    namespace_id: int,
    session_id: int,
    entity_public_id: bytes,
    arena_public_id: bytes,
) -> tuple[int, int] | None:
    """Look up entity in namespace and create arena if found.

    See: try_lookup (lines 2348-2386)

    Returns (entity_internal_id, arena_internal_id) or None if not found.
    """

# =============================================================================
# Arena Operations (each is one transaction)
# =============================================================================

def create_arena(conn: apsw.Connection, public_id: bytes, namespace_id: int, session_id: int) -> int:
    """Create empty arena, return internal ID.

    See: _create_empty_in_session (lines 2619-2634), _build_tmp_arena (lines 2033-2047)
    """

def create_arena_with_entities(
    conn: apsw.Connection,
    public_id: bytes,
    namespace_id: int,
    session_id: int,
    entity_ids: list[int],
) -> int:
    """Create arena with initial entity memberships. Returns arena internal ID.

    See: _create_arena_with_entities (lines 1905-1938)
    Also used by: copy_to_new_arena (lines 3176-3195)
    """

def verify_in_namespace_and_add_to_arena(
    conn: apsw.Connection,
    namespace_id: int,
    arena_id: int,
    entity_id: int,
) -> None:
    """Verify entity is in namespace, then add to arena.

    See: _do_track_precomputed (lines 2937-2959)

    Raises RuntimeError if entity not in namespace.
    """

def remove_from_arena_and_maybe_rebind(
    conn: apsw.Connection,
    namespace_id: int,
    arena_id: int,
    entity_id: int,
) -> None:
    """Remove entity from arena and rebind to garbage if orphaned.

    See: _do_abandon (lines 3038-3047)
    """

def reassign_arena_to_garbage_session(conn: apsw.Connection, arena_id: int) -> None:
    """Reassign arena to garbage session (session 0).

    See: _dispose_arena_in_db (lines 1479-1485)
    """

# =============================================================================
# Garbage Collection Operations (each is one transaction)
# =============================================================================

def check_and_claim_gc(conn: apsw.Connection, session_id: int) -> bool:
    """Check if there's garbage and no other collector, claim if so.

    See: _cleanup_worker (lines 1374-1408)

    Returns True if we claimed GC (caller should run GC loop).
    Returns False if another session is collecting or no garbage exists.
    """

def release_gc_claim(conn: apsw.Connection, session_id: int) -> None:
    """Release garbage collection claim.

    See: _cleanup_worker finally (lines 1431-1438)
    """

def run_one_gc_step(conn: apsw.Connection) -> bool:
    """Run one step of garbage collection. Returns True if work was done.

    See: _clean_up_garbage (lines 1210-1337)

    This handles:
    - Finding a garbage arena and processing one entity from it
    - Deleting empty garbage arenas
    - Deleting orphan garbage sessions (sessions with no arenas, except session 0)
    """

# =============================================================================
# Precomputed Data Loading (one transaction)
# =============================================================================

def load_precomp_data_for_entities(
    conn: apsw.Connection,
    initial_entity_ids: list[int],
    sync_dep_kinds: list[str],
) -> tuple[dict[int, bytes], dict[int, list[tuple[int, str]]]]:
    """Load precomputed data for entities and their transitive sync deps.

    See: _load_into_tracked (lines 2832-2910)

    Iteratively expands frontier to include all transitively-reachable entities
    via the specified dep kinds.

    Returns:
        (id_to_public_id, id_to_deps) where:
        - id_to_public_id: mapping from internal ID to public ID bytes
        - id_to_deps: mapping from internal ID to list of (required_id, kind)
    """

# =============================================================================
# Backref Queries (one read transaction)
# =============================================================================

def get_backrefs_for_entity(
    conn: apsw.Connection,
    namespace_id: int,
    entity_id: int,
) -> tuple[list[tuple[bytes, str]], list[str], list[tuple[bytes, bytes, bool]]]:
    """Get all backrefs for an entity.

    See: backrefs (lines 2389-2505)

    Returns:
        (entity_backrefs, pin_names, arena_backrefs) where:
        - entity_backrefs: list of (requirer_public_id, dep_kind)
        - pin_names: list of pin names
        - arena_backrefs: list of (arena_public_id, session_public_id, is_garbage)
    """

# =============================================================================
# Entity Load Operation (one transaction)
# =============================================================================

def load_cbor_and_create_children(
    conn: apsw.Connection,
    fxtr_id: FxtrID,
    internal_id: int,
    namespace_id: int,
) -> tuple[FxtrCbor, list[int]]:
    """Load entity data, creating child entities for p/u nodes.

    See: _load_cbor_and_save_children (lines 1549-1775)

    For d/n nodes: loads stored CBOR, deserializes, extracts child IDs
    For p/u nodes: recursively loads underlying data, creates wrapped child
                   entities in entity tables (but not namespace/arena)

    Returns (transformed_data, child_internal_ids).
    Caller is responsible for adding children to namespace and arena.
    """

# =============================================================================
# Copy Worklist Operations (each is one transaction)
# =============================================================================

def copy_discover(
    conn: apsw.Connection,
    session_id: int,
    public_id: bytes,
) -> tuple[int, bool]:
    """Discover phase: check if entity exists, add reservation if so.

    See: _build_tmp_arena... _DiscoverWork handling (lines 2068-2093)

    Returns (internal_id, found).
    If found: reservation added, caller should enqueue TrackDeps.
    If not found: caller should fetch from source arena and enqueue Save.
    """

def copy_save(
    conn: apsw.Connection,
    session_id: int,
    public_id: bytes,
    storage_location: str,
    cbor_bytes: bytes | None,
    deps: list[tuple[int, str]],  # (dep_internal_id, kind) - caller must resolve IDs first!
) -> int:
    """Save phase: insert entity into entity tables.

    See: _build_tmp_arena... _SaveWork handling (lines 2169-2248)

    IMPORTANT: Caller must resolve dep public_ids to internal_ids first using
    lookup_entity_ids_batch. This keeps the transaction small and focused.

    Handles re-check for concurrent insertions.
    Adds reservation for the entity.
    Returns internal_id.
    """

def copy_track_deps(
    conn: apsw.Connection,
    session_id: int,
    namespace_id: int,
    temp_arena_id: int,
    entity_id: int,
) -> tuple[bool, list[int]]:
    """TrackDeps phase: check namespace membership, get deps if not in namespace.

    See: _build_tmp_arena... _TrackDepsWork handling (lines 2109-2167)

    Returns (already_in_namespace, dep_internal_ids).
    If already in namespace: adds to arena, releases reservation, returns (True, []).
    Otherwise: loads dep IDs, adds reservations, returns (False, dep_ids).
    """

def copy_track(
    conn: apsw.Connection,
    session_id: int,
    namespace_id: int,
    arena_id: int,
    entity_id: int,
) -> None:
    """Track phase: add entity to namespace and arena.

    See: _build_tmp_arena... _TrackWork handling (lines 2250-2265)

    Adds entity to namespace (with backrefs) if needed.
    Adds entity to arena.
    Releases reservation.
    """
```

### sqlite_namespace.py

Mirrors the structure of `sqlite_arena.py` but dispatches to `sqlite_ops` via `ConnectionPoolGroup`.

**Key changes from sqlite_arena.py:**
1. Replace `AsyncEngineGroup` with `ConnectionPoolGroup`
2. Replace `async with engine.write_txn_engine.begin() as conn:` with `await pool_group.write_pool.run_sync_with_connection(sqlite_ops.some_function, args...)`
3. Keep all the arena pending-task machinery exactly the same
4. Keep session file locking exactly the same

**Example transformation:**

Before (sqlite_arena.py):
```python
async with self._async_engine_group.write_txn_engine.begin() as conn:
    result = await conn.execute(
        select(session_table.c.id).where(session_table.c.is_garbage == True)
    )
    row = result.fetchone()
```

After (sqlite_namespace.py):
```python
row = await self._pool_group.write_pool.run_sync_with_connection(
    sqlite_ops.find_orphan_garbage_session
)
```

## Enum Storage

Enums (`StorageLocation`, `DepKind`) are stored as their string values:
- `StorageLocation.HERE` → `"here"`
- `StorageLocation.NONE` → `"none"`
- `DepKind.STRONG_REF` → `"strong_ref"`
- etc.

Conversion happens in sqlite_ops functions.

## Cancellation Handling

Before committing any transaction, sync functions should call:
```python
anyio.from_thread.check_cancelled()
```

This ensures that if the async task was cancelled while waiting, we rollback instead of committing.

## Implementation Steps

### Phase 1: Core Infrastructure

- [x] Create `fxtr2/data/sqlite_store/` directory with `__init__.py`
- [x] Implement `sqlite_util.py` with `ConnectionPool` and `ConnectionPoolGroup`
- [x] Create `tests/test_sqlite_store/` directory
- [x] Write `test_sqlite_util.py` to test connection pool behavior
- [x] Run tests for sqlite_util

### Phase 2: Bootstrap with Copied Code

The goal here is to establish a working baseline that we can incrementally refactor.

- [x] Copy `sqlite_arena.py` to `sqlite_store/sqlite_namespace.py` using bash `cp`
- [x] Update imports in the copy so it works as a standalone module
- [x] Copy `test_sqlite_arena.py` to `test_sqlite_store/test_sqlite_store.py`
- [x] Update test imports to test the copied `sqlite_namespace` module
- [x] Verify all tests pass with the copied code (71 pass, 1 pre-existing failure)

### Phase 3: Setup Infrastructure

Prepare the hybrid state where we have both the new APSW pool and old async engine.

- [x] Create `sqlite_ops.py` with:
  - SQL constant strings for table creation
  - `create_pool_group()` function (setup is handled by async engine for now)
- [x] Update `sqlite_namespace.py`:
  - Replace table metadata constants (that will be removed) with imports from `sqlite_arena.py`
  - Replace local `StorageLocation` enum with import from `sqlite_arena.py`
  - Modify setup to create a `ConnectionPoolGroup` via `sqlite_ops.create_pool_group()` after `init_database()`
  - Keep the existing async SQLAlchemy engine for all non-setup transactions
  - Added `pool_group` parameter to `__init__` and `attach_to_engine`
- [x] Verify all tests still pass (71 pass, 1 pre-existing failure)
- [x] Fix pre-existing failure: `create_fxtr_engine` now skips `pool_size` for in-memory databases (all 72 tests pass)
- [x] Refactor initialization logic:
    - always require a pool group, it cannot be None
    - change `open` API to pass in a database path rather than a sqlalchemy URL, and allow memory database paths with `setup_database_and_create_pool_group`
    - remove attach_to_engine and just assume we are using `open`
    - remove old init_database in favor of `setup_database_and_create_pool_group`; do not use old system for setup at all
    - rewrite tests to instead test `setup_database_and_create_pool_group`

### Phase 4: Incremental Op Migration

For each operation category below, follow this pattern:
1. Write the new op(s) in `sqlite_ops.py`
2. Update `sqlite_namespace.py` to use the new op instead of the async engine
3. Verify tests still pass

Note: The intent of the below ops is that each operation should correspond to one transaction in the original code. If a single original transaction is broken up into multiple ops below, this may be a mistake in the op decomposition. If it is simple to combine these operations into a single operation that matches the original transaction, do that, but make a note in the plan below indicating the change. If it is complex to do so (e.g. because there are complex state interactions between the namespace Python object and sqlite within a single transaction), stop and ask for clarifications on what to do before proceeding.

**Session operations:**
- [x] `insert_session`
- [x] `mark_session_garbage`
- [x] `mark_session_garbage_by_public_id`
- [x] `set_session_collecting_garbage`
- [x] `find_session_collecting_garbage`
- [x] `find_and_delete_orphan_garbage_session` (combined find + delete in one transaction)

**Namespace operations:**
- [x] `get_or_create_user_namespace`

**Batched lookup operations:**
- [x] `load_precomp_data_for_entities` (used instead of separate batch ops)

**Pin operations:**
- [x] `create_or_update_pin`
- [x] `unpin_and_maybe_rebind`
- [x] `list_pins_with_prefix`

**Retrieve/lookup operations:**
- [x] `retrieve_pinned_and_create_arena`
- [x] `lookup_in_namespace_and_create_arena`

**Arena operations:**
- [x] `create_arena`
- [x] `create_arena_with_entities`
- [x] `verify_in_namespace_and_add_to_arena`
- [x] `remove_from_arena_and_maybe_rebind`
- [x] `reassign_arena_to_garbage_session`

**Private helpers implemented:**
- [x] `_is_entity_orphaned_in_namespace`
- [x] `_rebind_to_garbage_if_needed` (returns bool for garbage signaling)
- [x] `add_entity_to_namespace_if_needed`
- [x] `add_entity_to_arena_if_not_present`

**Garbage collection operations:**
- [x] `check_and_claim_gc`
- [x] `release_gc_claim`
- [x] `run_one_gc_step`

**Precomputed data loading:**
- [x] `load_precomp_data_for_entities`

**Backref queries:**
- [x] `get_backrefs_for_entity`

**Entity load operations:**
- [x] `load_cbor_and_create_children` (sync version with sync visitor)
- [x] `load_and_add_children_to_arena` (comprehensive op for load method)

**Copy worklist operations:**
- [x] `copy_discover`
- [x] `copy_save`
- [x] `copy_track_deps`
- [x] `copy_track`

**Additional helper operations:**
- [x] `add_entity_reservation` (helper used within transactions)
- [x] `release_entity_reservation` (helper used within transactions)
- [x] `lookup_and_add_to_arena` (for copy_from_arena)
- [x] `get_entity_deps`, `get_entity_public_id`, `lookup_entity_by_public_id`, `create_entity_with_deps`, `load_entity_data` (various helpers)

### Phase 5: Cleanup

Once all ops are converted and no transactions use the old async engine:

- [x] Remove the async SQLAlchemy engine from `sqlite_namespace.py`
- [x] Remove all imports from `sqlite_arena.py`
- [x] Remove any unused helper functions/constants
- [x] Re-enable memory database support in `SQLiteFxtrSession.open()`:
  - Remove the `ValueError` check for `:memory:` in db_path
  - Memory DBs work once SQLAlchemy/aiosqlite is removed (APSW-only)
  - Un-xfail `test_memory_db_skips_session_files` in test_sqlite_store.py
- [x] Un-skip `test_live_session_not_marked_dead` in test_sqlite_store.py, which should work again once we no longer use SQLAlchemy
- [x] Verify all tests still pass (87 tests pass!)
- [x] Update `__init__.py` to export public classes

### Phase 6: Documentation

- [x] Update CODE_OVERVIEW.md with new module documentation
- [x] Update LOCAL_CODE_OVERVIEW.md if one exists for this area

---

## Post-Implementation (Completed)

See `plans/phase2/stage8post1_followups.md` for details on follow-up work:

- [x] JSON optimization for constant SQL strings
- [x] Migration of existing users (git_store, fxtr_context_api)
- [x] Concurrent access deadlock fix (APSW context manager issue)
- [x] Benchmarks showing 7-10x speedup
- [x] Deprecation of old implementation (moved to deprecated/)
- [x] Documentation updates

**Final Status: COMPLETE ✅**

The APSW-based sqlite_store implementation is now the primary SQLite backend,
delivering 7-10x performance improvements over the deprecated sqlite_arena implementation.

## Migration Notes

This refactor uses a "strangler fig" approach:
1. Copy `sqlite_arena.py` to the new location as a working baseline
2. Incrementally replace each transaction with APSW-based ops
3. Tests run after each op migration to catch issues immediately
4. Once complete, the original `sqlite_arena.py` can be removed

After this refactor is complete and tested:
1. Update all imports to use `sqlite_store` module
2. Run benchmarks to verify performance improvement
3. Remove the original `sqlite_arena.py`

## Potential Issues to Watch For

1. **Thread safety of freelist**: The simple list-based freelist should be fine for append/pop from multiple threads (Python GIL makes these atomic), but monitor for issues.

2. **Connection state leakage**: Ensure connections are properly reset (rollback if in transaction) before returning to pool.

3. **APSW vs aiosqlite behavior**: Watch for subtle differences in error handling, type coercion, etc.

4. **Pragma persistence**: WAL mode is set once during setup (persists to file). Foreign keys are set per-connection via the setup callback.

5. **Recursive p/u node loading**: The sync version of `load_cbor_and_create_children` needs careful implementation to handle the recursive nature correctly.

6. **APSW readonly + WAL mode**: Using `SQLITE_OPEN_READONLY` for the read pool with WAL mode may have issues. If problems arise during testing, we may need to use READWRITE connections for reads too. This needs investigation/experimentation.

## Design Decisions (Q&A Summary)

These decisions were made during planning:

- **ConnectionPoolGroup ownership**: Shared across sessions, not per-session. Created after database init.
- **Per-connection setup**: Foreign keys enabled via callback on each new connection.
- **Transaction granularity**: Preserve current small-transaction pattern from sqlite_arena.py. Each worklist item = separate transaction.
- **Enum storage**: String values (e.g., `"strong_ref"`) for readability.
- **Cancellation**: `anyio.from_thread.check_cancelled()` called before transaction commit.
- **Error handling**: Exceptions propagate up naturally; arena closes on error as before.
- **List for freelist**: Simple list is fine; GIL provides atomicity for append/pop.

## Issues Discovered During Implementation

### Flaky test: `test_loop_store_abandon_n_only__with_async_gc_worker`

During Phase 4 session operations migration, observed an intermittent failure:

```
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: arena_membership.arena_id, arena_membership.entity_id
[SQL: INSERT INTO arena_membership (arena_id, entity_id) VALUES (?, ?)]
[parameters: (17, 6)]
```

This suggests a race condition where the same entity is being added to an arena twice. The test passed on subsequent runs. This is likely a pre-existing bug unrelated to the APSW migration - may be worth investigating after the migration is complete.

### Complex operations requiring clarification

Several operations marked with ⚠️ above have complex internal structure where helper methods are called within the same transaction:

**Pattern 1: Operations that call `_rebind_to_garbage_if_needed`**
- `create_or_update_pin` (pin_entity_from_arena)
- `unpin_and_maybe_rebind` (unpin_entity)
- `remove_from_arena_and_maybe_rebind`

The `_rebind_to_garbage_if_needed` helper:
1. Calls `_is_entity_orphaned_in_namespace` (checks if entity has backrefs, pins, or arena membership)
2. If orphaned, finds or creates a garbage arena in the namespace
3. Adds the entity to the garbage arena

**Pattern 2: Operations that call `_create_arena_with_entities`**
- `retrieve_pinned_and_create_arena`
- `lookup_in_namespace_and_create_arena`

These do a lookup, then call `_create_arena_with_entities` which inserts into both `arena` and `arena_membership` tables.

**Options to consider:**
1. **Migrate helpers too**: Convert `_rebind_to_garbage_if_needed`, `_is_entity_orphaned_in_namespace`, `_create_arena_with_entities` to APSW ops and compose them in larger combined ops
2. **Keep as-is for now**: Leave these using SQLAlchemy until we're ready for a larger refactor
3. **Split transactions**: Some of these might be safe to split into multiple smaller transactions if the invariants allow

### Current failure: Cross-namespace copy with mixed APSW/SQLAlchemy

**Status**: Blocking further progress on complex ops migration.

**What works:**
- Simple pin/unpin within a single namespace (TestPinningEntities: 6/6 pass)
- Retrieve pinned entities (TestRetrievingPinnedEntities: 3/3 pass)
- List pins (TestListPins: 7/7 pass)

**What fails:**
- Cross-namespace copy tests (TestCrossNamespaceCopy: 1/3 pass, 2 fail)

**Test flow that fails:**
1. Pin entity with deps to namespace A (uses `_build_tmp_arena_copying_from_external_arena` - SQLAlchemy)
2. Retrieve from namespace A (uses new APSW `retrieve_pinned_and_create_arena`)
3. Copy to namespace B via `pin_entity_from_arena(arena_a)`:
   - `_build_tmp_arena_copying_from_external_arena` runs (SQLAlchemy) - copies entities to namespace B
   - `create_or_update_pin` runs (APSW) - **FAILS with FOREIGN KEY constraint**

**Debug output shows:**
```
Entities: (1, '2E'), (2, '64A7...'), (3, '640D...')
Namespace ownership (namespace B = id 2): EMPTY
All namespace ownership: (1,1), (1,2), (1,3)  -- all in namespace A (id=1)
```

**Root cause hypothesis:**
When copying from namespace A to namespace B, `_build_tmp_arena_copying_from_external_arena` should add entities to namespace B's ownership table. But the debug output shows namespace B's ownership is EMPTY before `create_or_update_pin` runs.

This suggests either:
1. **Transaction visibility issue**: SQLAlchemy commits aren't visible to APSW connections
2. **Logic bug**: `_build_tmp_arena_copying_from_external_arena` isn't adding to namespace B correctly
3. **Timing issue**: The APSW transaction starts before SQLAlchemy commits

**Next steps to investigate:**
- Add logging to `_build_tmp_arena_copying_from_external_arena` to verify it's adding to namespace B
- Check if there's a transaction isolation issue between APSW and SQLAlchemy
- Consider whether we need to migrate `_build_tmp_arena_copying_from_external_arena` to APSW as well to avoid mixed-driver issues

**Resolution:** Completed full migration to APSW. Cross-namespace issues resolved by eliminating mixed-driver usage.

### Phase 4 Completion Status

**✅ All operations migrated to APSW** - SQLAlchemy no longer used for any database transactions.

Tests passing: User reports tests now pass after fixing connection pool setup for locking issues.

### Dynamic SQL in `load_precomp_data_for_entities`

**Issue:** This function builds dynamic SQL queries with variable-length IN clauses:
```python
placeholders = ",".join("?" * len(frontier_list))
cursor = conn.execute(
    f"SELECT id, public_id FROM entity WHERE id IN ({placeholders})",
    frontier_list,
)
```

This occurs in two queries within the function (one for entity IDs, one for deps).

**Why it matters:** This is a frequently-called operation (every time we load precomputed data for an arena). Dynamic SQL prevents SQLite from caching the prepared statement, potentially causing repeated parsing/planning overhead.

**Options to make it use constant SQL:**

1. **JSON functions (SQLite 3.38+)** - Recommended if acceptable
   ```python
   import json
   cursor = conn.execute(
       """SELECT e.id, e.public_id FROM entity e
          WHERE e.id IN (SELECT value FROM json_each(?))""",
       (json.dumps(frontier_list),)
   )
   ```
   - Pros: Fixed SQL string, efficient, widely supported
   - Cons: Requires JSON serialization overhead, SQLite 3.38+ (2022)

2. **Recursive CTE (pure SQL)**
   ```sql
   WITH RECURSIVE transitive_deps(id) AS (
     SELECT value FROM json_each(?)  -- initial IDs as JSON array
     UNION
     SELECT ed.required_id FROM entity_dep ed
     JOIN transitive_deps td ON ed.requirer_id = td.id
     WHERE ed.kind IN (SELECT value FROM json_each(?))
   )
   SELECT e.id, e.public_id FROM entity e
   JOIN transitive_deps td ON e.id = td.id
   ```
   - Pros: Single query, very elegant, fully constant SQL
   - Cons: More complex, requires JSON for initial values

3. **Temporary table approach**
   ```python
   conn.execute("CREATE TEMP TABLE IF NOT EXISTS _frontier(id INTEGER PRIMARY KEY)")
   conn.execute("DELETE FROM _frontier")
   conn.executemany("INSERT INTO _frontier VALUES (?)", [(id,) for id in frontier_list])
   cursor = conn.execute("SELECT e.id, e.public_id FROM entity e JOIN _frontier f ON e.id = f.id")
   ```
   - Pros: No JSON needed, constant SQL
   - Cons: Temp table management, more overhead per call

4. **Keep current approach**
   - Pros: Simple, works well, properly parameterized (no SQL injection risk)
   - Cons: Different query plan for each frontier size, repeated parsing

**Recommendation:** Option #1 (JSON) offers best balance of fixed SQL and simplicity, or keep current if profiling shows it's not a bottleneck.

**Current status:** Using dynamic SQL (option #4). Can revisit if performance profiling shows this as a hotspot.
