# Extra 3: SQLite Read-Only Mode

## Goal

Add a `read_only` mode to the SQLite store. In this mode:

- No session is tracked in the DB; no session files are created or locked
- No garbage collection runs
- Pin, unpin, register_indexer, and unregister_indexer all raise an error
- Read operations (retrieve, list_pins, iter_matches, sql, try_lookup, backrefs, list_indexers) work normally
- The SQLite connection is opened with `mode=ro` as defense-in-depth
- No write connection pool is created at all

The intent is that read-only sessions are fully invisible to other processes and cannot corrupt database state, even on a network filesystem with insufficient locking guarantees.

## Design

### User-Facing API

`SQLiteFxtrNamespace` gains a `read_only: bool = False` parameter:

```python
async with SQLiteFxtrNamespace("path/to/db.sqlite", "myns", read_only=True) as ns:
    ref = await ns.retrieve("my_pin", MyEntity)
    # ns.pin(...) would raise RuntimeError
```

### Connection Layer Changes

**`ConnectionWorkerPoolGroup`** (`sqlite_util.py`): Make `write_pool` optional:

```python
write_pool: ConnectionWorkerPool | None
```

Update `close()` and `aclose()` to skip the write pool when it's `None`.

**`sqlite_ops.py`**: Add a new read-only setup function:

```python
def setup_readonly_pool_group(db_path: str, read_pool_size: int = 5) -> ConnectionWorkerPoolGroup:
```

This function:
- Validates that the DB file exists (raises `FileNotFoundError` if not)
- Opens a connection with `mode=ro` via URI (`sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, ...)`)
- Reads `PRAGMA user_version` and validates it equals `DB_USER_VERSION` (raises `ValueError` if wrong version or uninitialized)
- Does NOT run migrations, create tables, create the garbage session, or truncate segments
- Creates a `SegmentManager` for reading segment files
- Creates a read-only `ConnectionWorkerPool` (whose connections use `mode=ro`)
- Returns a `ConnectionWorkerPoolGroup` with `write_pool=None`

For the read connection setup, we add `_connection_setup_readonly` that sets `busy_timeout` and `foreign_keys` but skips anything write-related. (In practice `_connection_setup` is already fine since it only sets pragmas that work on read-only connections.)

Since the connection pool uses `sqlite3.connect(self._db_path, ...)`, we need to pass the URI-style path to the pool and enable `uri=True`. The simplest approach: add an optional `uri: bool = False` parameter to `ConnectionWorkerPool.__init__`, passed through to `sqlite3.connect`.

### Pool Group Manager

`_SharedSqlitePoolGroupManager` currently keys on `db_path`. Read-only pool groups should NOT be shared with read-write pool groups (different connection modes). Two options:

1. Key on `(db_path, read_only)` — allows sharing among multiple concurrent read-only sessions on the same DB.
2. Bypass the manager entirely for read-only sessions.

Option 1 is cleanest: read-only sessions on the same DB share a SegmentManager and connection pool, just like read-write sessions do. The manager's `get_pool_group` method gains a `read_only: bool = False` parameter and includes it in the dict key.

### Session Layer Changes

**`SQLiteFxtrSession`** gains `read_only: bool = False`:

- Stored as `self._read_only`
- `is_active()`: in read-only mode, returns `not self._should_close_evt.is_set()` (no `_internal_sid` check since we never insert a session)
- `start()`: in read-only mode, sets up the `_finalizer` and `_index_computer` but skips session insertion, session file creation, lifecycle handler, and garbage worker. We still need `_index_computer` for query resolution.
- `aclose()` / `close()`: in read-only mode, just sets the close event and detaches the finalizer (no DB writes)
- `create_or_retrieve_user_namespace()`: in read-only mode, calls a new read-only SQL function `get_user_namespace` that only does a SELECT (raises `KeyError` if namespace doesn't exist)

**`SQLiteNamespaceBackend`**: Needs to know if it's read-only. The session already carries `_read_only`, so the backend checks `self._session._read_only`.

- `pin_entity_from_arena()`: raises `RuntimeError("Store is open in read-only mode")` if read-only
- `unpin_entity()`: same error
- `register_indexer()`: same error
- `unregister_indexer()`: same error
- `export_archive()`: same error (it creates a new DB internally, which is a write)
- All read operations work unchanged — they only use `read_pool`

**`FxtrNamespace`** (in `fxtr_context_api.py`): The `pin()` and `unpin()` methods delegate to the backend, which will raise. We could also add an early guard at the `FxtrNamespace` level, but since the backend already raises, it's redundant. Keeping the guard only in the backend avoids duplicating the check.

### SQL Layer

**`sqlite_ops.py`**: Add a new function:

```python
def get_user_namespace(conn: sqlite3.Connection, name: str) -> tuple[int, uuid.UUID]:
    """Look up a user namespace by name (read-only, no creation)."""
    cursor = conn.execute(
        "SELECT id, public_id FROM namespace WHERE name = ? AND kind = 'user'",
        (name,)
    )
    row = cursor.fetchone()
    if row is None:
        raise KeyError(f"Namespace '{name}' not found")
    return row[0], uuid.UUID(bytes=bytes(row[1]))
```

### `SQLiteFxtrSession.open()` Changes

The `open()` classmethod needs a `read_only` parameter. In read-only mode:
- Uses `GLOBAL_POOL_GROUP_MANAGER.get_pool_group(db_path, read_only=True)` to get a read-only pool group
- In `attach_to_connection_pool_group`: only starts read workers (skips write workers since `write_pool` is None)
- Passes `read_only=True` to the session constructor

## Implementation Steps

- [x] 1. **`sqlite_util.py`**: Make `ConnectionWorkerPoolGroup.write_pool` optional (`ConnectionWorkerPool | None`). Update `close()` and `aclose()` to handle `None`. Add `uri: bool = False` parameter to `ConnectionWorkerPool.__init__`, passed through to `sqlite3.connect`.

- [x] 2. **`sqlite_ops.py`**: Add `setup_readonly_pool_group()` function. Add `get_user_namespace()` function. Add `async_setup_readonly_pool_group()` wrapper.

- [x] 3. **`sqlite_namespace.py` — pool manager**: Update `_SharedSqlitePoolGroupManager.get_pool_group()` to accept `read_only: bool = False` and key on `(db_path, read_only)`. When `read_only=True`, use the new readonly setup function.

- [x] 4. **`sqlite_namespace.py` — session**: Add `read_only` to `SQLiteFxtrSession.__init__`. Update `is_active()`, `start()`, `aclose()`, `close()`, `create_or_retrieve_user_namespace()` for read-only mode. Update `open()` and `attach_to_connection_pool_group()` to accept and pass through `read_only`.

- [x] 5. **`sqlite_namespace.py` — backend**: Add read-only guards to `pin_entity_from_arena()`, `unpin_entity()`, `register_indexer()`, `unregister_indexer()`, `export_archive()`.

- [x] 6. **`fxtr_context_api.py`**: Add `read_only: bool = False` to `SQLiteFxtrNamespace.__init__`. Pass through to `SQLiteFxtrSession.open()`.

- [x] 7. **Tests**: Add tests for read-only mode:
  - Opening a read-only session on an existing DB with pins, retrieving and listing pins
  - Verifying pin/unpin/register_indexer/unregister_indexer raise appropriate errors
  - Verifying no session row is created in the DB
  - Verifying no session files are created
  - Verifying read-only session works when another read-write session is active on the same DB
  - Verifying read-only session fails cleanly when DB doesn't exist
  - Verifying read-only session fails cleanly when DB has wrong schema version

- [x] 8. **Update CODE_OVERVIEW.md**

## Error Message

All write operations in read-only mode raise:

```
RuntimeError("Store is open in read-only mode")
```
