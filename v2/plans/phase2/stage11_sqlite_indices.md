# Stage 11: SQLite Performance Indices

## Problem

The SQLite schema in `sqlite_ops.py` is missing several indices that would speed up common query patterns, particularly for garbage collection and orphan detection.

## Analysis

### Current Indices

The schema has these indices (explicit or implicit from UNIQUE/PK constraints):

| Table | Index |
|-------|-------|
| `entity` | PK(id), UNIQUE(public_id) |
| `entity_dep` | UNIQUE(requirer_id, required_id, kind), INDEX(requirer_id) |
| `stored_entity_data` | PK(entity_id) |
| `namespace` | PK(id), UNIQUE(public_id) |
| `namespace_entity_ownership` | UNIQUE(namespace_id, entity_id) |
| `namespace_entity_backref` | UNIQUE(requirer_id, required_id, namespace_id, kind), INDEX(required_id, namespace_id) |
| `namespace_pin` | PK(id), UNIQUE(namespace_id, name) |
| `session` | PK(id), UNIQUE(public_id) |
| `arena` | PK(id), UNIQUE(public_id) |
| `arena_membership` | UNIQUE(arena_id, entity_id) |
| `entity_reservations` | UNIQUE(session_id, entity_id) |

### Missing Indices (High Priority)

#### 1. `arena(session_id)`

Used heavily in garbage collection queries that join `arena` to `session`:

```python
# run_one_gc_step
SELECT a.id, a.namespace_id FROM arena a
JOIN session s ON a.session_id = s.id
WHERE s.is_garbage = 1

# find_and_delete_orphan_garbage_session
NOT EXISTS (SELECT 1 FROM arena WHERE arena.session_id = session.id)
```

#### 2. `arena(namespace_id)`

Used in rebind-to-garbage lookups:

```python
# _rebind_to_garbage_if_needed
SELECT id FROM arena WHERE namespace_id = ? AND session_id = ? LIMIT 1
```

#### 3. `arena_membership(entity_id)`

Used in orphan detection. The existing UNIQUE index is on `(arena_id, entity_id)`, but queries filter by `entity_id` first:

```python
# _is_entity_orphaned_in_namespace
SELECT 1 FROM arena_membership am
JOIN arena a ON am.arena_id = a.id
WHERE am.entity_id = ? AND a.namespace_id = ?
```

#### 4. `namespace_pin(entity_id, namespace_id)`

Used in orphan checks and backref queries:

```python
# _is_entity_orphaned_in_namespace
SELECT 1 FROM namespace_pin WHERE entity_id = ? AND namespace_id = ?

# get_backrefs_for_entity
SELECT name FROM namespace_pin WHERE entity_id = ? AND namespace_id = ?
```

### Missing Indices (Lower Priority)

#### 5. `namespace(name, kind)`

Used in namespace lookup. Probably low-volume but included for completeness:

```python
# get_or_create_user_namespace
SELECT id, public_id FROM namespace WHERE name = ? AND kind = 'user'
```

## Solution

### 1. Add Indices to Schema

Add these `CREATE INDEX` statements to `SQL_CREATE_TABLES` so fresh databases get them:

```sql
CREATE INDEX IF NOT EXISTS ix_arena_session ON arena(session_id)
CREATE INDEX IF NOT EXISTS ix_arena_namespace ON arena(namespace_id)
CREATE INDEX IF NOT EXISTS ix_arena_membership_entity ON arena_membership(entity_id)
CREATE INDEX IF NOT EXISTS ix_namespace_pin_entity ON namespace_pin(entity_id, namespace_id)
CREATE INDEX IF NOT EXISTS ix_namespace_name_kind ON namespace(name, kind)
```

### 2. Create Migration System

Create `sqlite_migrations.py` to handle upgrading existing v2 databases to v3.

Unlike the v1→v2 migration (which required table renames and data copying), this migration is simple: just run `CREATE INDEX IF NOT EXISTS` statements.

### 3. Migration System Design

```python
# sqlite_store/sqlite_migrations.py

def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add performance indices."""
    with transaction(conn, "IMMEDIATE"):
        conn.execute("CREATE INDEX IF NOT EXISTS ix_arena_session ON arena(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_arena_namespace ON arena(namespace_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_arena_membership_entity ON arena_membership(entity_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_namespace_pin_entity ON namespace_pin(entity_id, namespace_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_namespace_name_kind ON namespace(name, kind)")
        conn.execute("PRAGMA user_version = 3")

# Registry: (from_version, to_version) -> migration_fn
_MIGRATIONS: dict[tuple[int, int], Callable[[sqlite3.Connection], None]] = {
    (2, 3): migrate_v2_to_v3,
}

def run_migrations(conn: sqlite3.Connection, current_version: int, target_version: int) -> None:
    """Run all migrations from current_version to target_version."""
    version = current_version
    while version < target_version:
        next_version = version + 1
        migration_fn = _MIGRATIONS.get((version, next_version))
        if migration_fn is None:
            raise ValueError(
                f"No migration from version {version} to {next_version}. "
                f"Cannot upgrade database from {current_version} to {target_version}."
            )
        migration_fn(conn)
        version = next_version
```

Key differences from the deprecated SQLAlchemy-based migrations:
- Uses raw `sqlite3.Connection` instead of `AsyncConnection`
- Synchronous (matches the rest of `sqlite_ops.py`)
- Simpler registry: `{from_version: migration_fn}` instead of `{(from, to): fn}`

### 4. Integration with `setup_database_and_create_pool_group`

Change the version check logic from:

```python
elif current_version < DB_USER_VERSION:
    raise ValueError("Migrations are not supported right now.")
```

To:

```python
elif current_version < DB_USER_VERSION:
    from fxtr2.data.sqlite_store.sqlite_migrations import run_migrations
    run_migrations(setup_conn, current_version, DB_USER_VERSION)
```

## Implementation Steps

- [x] Add the 5 new `CREATE INDEX` statements to `SQL_CREATE_TABLES` in `sqlite_ops.py`
- [x] Bump `DB_USER_VERSION` from 2 to 3 in `sqlite_ops.py`
- [x] Create `sqlite_store/sqlite_migrations.py` with:
  - [x] `migrate_v2_to_v3()` function
  - [x] `_MIGRATIONS` registry
  - [x] `run_migrations()` function
- [x] Update `setup_database_and_create_pool_group()` to call migrations instead of raising
- [x] Add test for migration v2→v3 in `tests/test_sqlite_migrations.py`
- [x] Update CODE_OVERVIEW.md

## Files Changed

- `fxtr2/data/sqlite_store/sqlite_ops.py` - Schema changes, version bump, migration integration
- `fxtr2/data/sqlite_store/sqlite_util.py` - Added `connection_teardown` callback support
- `fxtr2/data/sqlite_store/sqlite_migrations.py` - New file with migration logic
- `tests/test_sqlite_migrations.py` - New file with migration tests

## Additional: PRAGMA optimize

Added `PRAGMA optimize` support to keep SQLite query statistics up to date:

1. **Connection teardown callback**: Added `connection_teardown` parameter to `ConnectionPool` in `sqlite_util.py`. This callback is invoked just before each connection is closed.

2. **`_connection_teardown()` in sqlite_ops.py**: Runs `PRAGMA optimize` on each connection before closing. Per SQLite docs: "Applications should be prepared to run PRAGMA optimize once per database connection just prior to closing the connection."

3. **Post-migration optimize**: `run_migrations()` runs `PRAGMA optimize` after completing all migrations, helping SQLite build statistics for newly created indices.

See: https://www.sqlite.org/pragma.html#pragma_optimize
