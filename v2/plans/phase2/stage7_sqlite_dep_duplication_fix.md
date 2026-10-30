# Stage 7: SQLite Dependency Duplication Fix

## Problem

Entities that reference the same child entity multiple times (e.g., 2 weak links + 1 strong link to the same child) would fail when stored in SQLite.

### Root Cause

The `entity_dep` table had a unique constraint on `(requirer_id, required_id)`, which didn't include the `kind` column. When an entity had multiple references to the same child with different link types, the system would try to insert multiple rows with the same `(requirer_id, required_id)` pair, causing a constraint violation.

Similarly, `namespace_entity_backref` didn't have a `kind` column at all, so it couldn't distinguish between different types of references to the same entity.

### Example Failure Case

```python
# This would fail:
parent = {
    "$fxtr.ctx": ...,
    "weak_ref1": RawWeakFxtrID(inner_id=child_id),  # First weak ref
    "weak_ref2": RawWeakFxtrID(inner_id=child_id),  # Second weak ref (same child)
    "strong_ref": RawStrongFxtrID(inner_id=child_id),  # Strong ref (same child)
}
```

The error was:
```
UNIQUE constraint failed: entity_dep.requirer_id, entity_dep.required_id
```

## Solution

### 1. Schema Changes

**`entity_dep` table:**
- Changed unique constraint from `(requirer_id, required_id)` to `(requirer_id, required_id, kind)`
- This allows multiple references to the same entity with different link types

**`namespace_entity_backref` table:**
- Added `kind` column
- Changed unique constraint from `(requirer_id, required_id, namespace_id)` to `(requirer_id, required_id, namespace_id, kind)`

### 2. Deduplication in `fetch_data_and_deps_from_arena()`

Added deduplication logic in `arena_util.py` so that identical `(dep_id, kind)` pairs are only returned once, even if an entity contains multiple references to the same child with the same link type.

### 3. Database Migration

Created a migration system to upgrade existing v1 databases to v2.

## Files Changed

- `fxtr2/data/arena_util.py` - Added deduplication with `seen` set
- `fxtr2/data/sqlite_arena.py` - Schema changes, version bump, migration integration
- `fxtr2/data/sqlite_arena_migrations.py` - New file with migration logic

## Database Migration System

### Overview

The migration system is designed to be simple and maintainable:

- **Version tracking**: Uses SQLite's `PRAGMA user_version` to track schema version
- **Automatic migration**: `init_database()` checks the version and runs migrations if needed
- **Lazy import**: The migrations module is only imported when a migration is actually needed
- **Table definitions**: Migrations use SQLAlchemy table definitions from `sqlite_arena.py` to create new tables, ensuring schema consistency

### How It Works

1. On database initialization, `init_database()` checks `PRAGMA user_version`
2. If version is 0 (fresh database): create all tables, set version to current
3. If version < current: import `sqlite_arena_migrations` and run `run_migrations()`
4. If version > current: raise error (database is from newer code)
5. If version == current: nothing to do

### Migration File Structure

```python
# sqlite_arena_migrations.py

# Import current table definitions (with version suffix for documentation)
from fxtr2.data.sqlite_arena import (
    entity_dep_table as entity_dep_table__changed_at_v2,
    namespace_entity_backref_table as namespace_entity_backref_table__changed_at_v2,
    DB_USER_VERSION,
)

# Assert to catch version mismatches during development
assert DB_USER_VERSION == 2, "check imports when changing version"

async def migrate_v1_to_v2(conn: AsyncConnection) -> None:
    """Migrate from v1 to v2."""
    # 1. Drop old indexes (SQLite indexes are database-wide)
    # 2. Rename old table to _tablename_v1
    # 3. Create new table using current schema definition
    # 4. Copy data from old table (with any transformations)
    # 5. Drop old table
    ...

# Registry of migrations
_MIGRATIONS = {
    (1, 2): migrate_v1_to_v2,
}

async def run_migrations(conn, current_version, target_version) -> None:
    """Run all migrations from current to target version."""
    ...
```

### SQLite-Specific Considerations

- **Indexes are database-wide**: When renaming a table, its indexes keep their names. Must drop old indexes before creating new table with same index names.
- **No ALTER CONSTRAINT**: SQLite doesn't support modifying constraints. Must use the "rename old, create new, copy data, drop old" pattern.
- **Foreign keys**: Temporarily disabled during migration or handled carefully.

### Adding Future Migrations

1. Bump `DB_USER_VERSION` in `sqlite_arena.py`
2. Update the assert in `sqlite_arena_migrations.py`
3. Update import aliases if table definitions changed
4. Add new migration function `migrate_vN_to_vM()`
5. Add entry to `_MIGRATIONS` dict
6. Add test in `test_sqlite_arena_migrations.py`

## Tests Added

- `tests/test_memory_arena.py::TestEdgeCases::test_entity_with_multiple_refs_to_same_child`
- `tests/test_sqlite_arena.py::TestMultipleRefsToSameChild::test_entity_with_multiple_refs_to_same_child`
- `tests/test_sqlite_arena_migrations.py::TestDatabaseMigrations::test_migrate_v1_to_v2`
- `tests/test_sqlite_arena_migrations.py::TestDatabaseMigrations::test_fresh_database_gets_current_version`
- `tests/test_sqlite_arena_migrations.py::TestDatabaseMigrations::test_future_version_raises_error`
