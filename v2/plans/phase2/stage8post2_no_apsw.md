# Stage 8 Post-2: Migration from APSW to Built-in sqlite3

This document describes the migration from the `APSW` SQLite wrapper to Python's built-in `sqlite3` module with manual transaction control.

## Overview

The `sqlite_store` module and its tests were migrated from using `APSW` to the standard library `sqlite3` module. This change simplifies dependencies and uses Python's built-in SQLite support, while maintaining manual transaction control via `isolation_level = None`.

## Key Changes

### 1. Module Replacement

**Before:**
```python
import apsw
conn = apsw.Connection(db_path)
```

**After:**
```python
import sqlite3
conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
```

### 2. Manual Transaction Control

All connections use `isolation_level=None` to disable autocommit mode, requiring explicit `BEGIN`, `COMMIT`, and `ROLLBACK` statements. This matches the previous APSW behavior where transactions were always explicit.

### 3. API Differences

#### Connection Methods

- **`conn.execute()`**: Works the same, but returns a cursor (not the connection itself)
- **`conn.executescript()`**: Avoided due to auto-commit behavior; multi-statement SQL is split into individual statements
- **`conn.in_transaction`**: Property (not method) that indicates if a transaction is active
- **`conn.last_insert_rowid()`**: Replaced with `cursor.lastrowid` after `cursor = conn.execute(...)`
- **`conn.changes()`**: Replaced with `cursor.rowcount` after `cursor = conn.execute(...)`

#### Exception Handling

- **`apsw.BusyError`** → **`sqlite3.OperationalError`** with `e.sqlite_errorcode == sqlite3.SQLITE_BUSY`
- **`apsw.ReadOnlyError`** → **`sqlite3.OperationalError`** with `e.sqlite_errorcode == sqlite3.SQLITE_READONLY`

#### Connection Configuration

- **`conn.config(apsw.SQLITE_DBCONFIG_DQS_DML, 0)`**: Not supported by `sqlite3` (double-quoted strings are disabled by default)
- **`conn.config(apsw.SQLITE_DBCONFIG_DQS_DDL, 0)`**: Not supported by `sqlite3`
- **`conn.set_busy_timeout(500)`** → **`conn.execute("PRAGMA busy_timeout = 500")`**

### 4. Connection Pooling

The `ConnectionPool` class was updated to work with `sqlite3.Connection`:

- Removed `readonly` parameter (read-only mode is no longer enforced; reads go through `read_pool` by convention)
- URI mode handling: For paths starting with `file:`, explicitly pass `uri=True` to `sqlite3.connect()` (needed for memory databases with shared cache)
- Connection return logic: Uses `if conn.in_transaction:` before rolling back uncommitted transactions

### 5. Schema Initialization

The `SQL_CREATE_TABLES` constant was refactored from a single multi-statement string into a `list[str]`, where each element is a single SQL statement. This avoids the need for `executescript()` (which auto-commits) and allows proper transaction control:

```python
# Before: Single string with semicolons
SQL_CREATE_TABLES = "CREATE TABLE ...; CREATE TABLE ...;"

# After: List of individual statements
SQL_CREATE_TABLES = [
    "CREATE TABLE ...",
    "CREATE TABLE ...",
]
```

Each statement is executed individually within a transaction.

## Files Modified

### Core Implementation

1. **`fxtr2/data/sqlite_store/sqlite_util.py`**
   - Replaced `import apsw` with `import sqlite3`
   - Updated `ConnectionPool` to use `sqlite3.Connection`
   - Removed `readonly` parameter and related URI handling
   - Updated `transaction` context manager for `sqlite3` exception handling
   - Added `if conn.in_transaction:` checks before rollback

2. **`fxtr2/data/sqlite_store/sqlite_ops.py`**
   - Replaced `import apsw` with `import sqlite3`
   - Updated `_connection_setup()` to use `sqlite3` PRAGMA statements
   - Refactored `SQL_CREATE_TABLES` from string to list
   - Updated all function signatures to use `sqlite3.Connection`
   - Replaced `conn.last_insert_rowid()` with `cursor.lastrowid`
   - Replaced `conn.changes()` with `cursor.rowcount`
   - Fixed URI mode handling for memory databases in `setup_database_and_create_pool_group()`

### Tests

3. **`tests/test_sqlite_store/test_sqlite_util.py`**
   - Updated all connection creation to use `sqlite3.connect()`
   - Removed readonly pool tests; replaced with tests verifying pools can read and write
   - Updated exception handling to use `sqlite3.OperationalError` with error codes

4. **`tests/test_sqlite_store/test_sqlite_store.py`**
   - Updated connection creation in `TestDatabaseInitialization` tests
   - Removed readonly URI connections (`mode=ro`); now use regular read-write connections

## Issues Encountered and Resolved

1. **`AttributeError: 'sqlite3.Connection' object has no attribute 'lastrowid'`**
   - **Issue**: Used `conn.lastrowid` instead of `cursor.lastrowid`
   - **Fix**: Always use `cursor.lastrowid` after `cursor = conn.execute(...)`

2. **`sqlite3.ProgrammingError: You can only execute one statement at a time`**
   - **Issue**: `conn.execute()` cannot handle multiple statements
   - **Fix**: Split `SQL_CREATE_TABLES` into a list of individual statements

3. **`sqlite3.OperationalError: cannot rollback - no transaction is active`**
   - **Issue**: Exception handler tried to rollback after `executescript()` auto-committed
   - **Fix**: Added `if conn.in_transaction:` check before rollback

4. **Memory database connections not seeing tables**
   - **Issue**: For memory databases with URI format (`file:name?mode=memory&cache=shared`), `sqlite3.connect()` needs explicit `uri=True`
   - **Fix**: Check if `db_path.startswith("file:")` and pass `uri=True` to `sqlite3.connect()`

5. **Read-only mode removed**
   - **Issue**: Read-only URI connections (`mode=ro`) had issues with table visibility
   - **Resolution**: Removed readonly enforcement entirely; both pools use read-write connections, with reads going through `read_pool` by convention

## Testing

All 87 tests in `tests/test_sqlite_store/` pass after the migration:
- `test_sqlite_util.py`: 13 tests
- `test_sqlite_store.py`: 74 tests

## Benefits

1. **Reduced dependencies**: No longer requires the `apsw` package
2. **Standard library**: Uses Python's built-in SQLite support
3. **Simpler code**: Removed readonly mode complexity
4. **Same functionality**: All features work identically with manual transaction control

## Notes

- Manual transaction control (`isolation_level=None`) is maintained throughout, ensuring explicit `BEGIN`/`COMMIT`/`ROLLBACK` control
- The `transaction` context manager continues to work the same way, handling busy errors with retries
- Connection pooling behavior is unchanged; both read and write pools now use read-write connections
