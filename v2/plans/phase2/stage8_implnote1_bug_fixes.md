# Stage 8 Implementation Note: Bug Fixes

This document describes bugs encountered during the APSW migration, the experiments
run to diagnose them, and the resolutions.

## Bug 1: Memory Databases Not Supported

### Symptom

When opening a `SQLiteFxtrSession` with a shared-cache memory database
(`file::memory:?cache=shared`), the session fails with "no such table: session".

### Diagnosis

The issue is that **APSW and aiosqlite/sqlite3 link against different SQLite libraries**:

```
sqlite3 (used by aiosqlite): SQLite 3.47.1 (system library)
APSW: SQLite 3.51.2 (bundled with APSW)
```

Because they use different SQLite instances, shared-cache memory databases created
by APSW cannot be accessed by aiosqlite - they have completely separate in-memory
namespaces.

### Resolution

Memory databases are temporarily blocked during the APSW migration:

1. Added a check in `SQLiteFxtrSession.open()` that raises `ValueError` if the
   path contains `:memory:`
2. Marked the `test_memory_db_skips_session_files` test as xfail
3. Added a note to Phase 5 (Cleanup) to re-enable memory database support once
   SQLAlchemy is fully removed

Once all operations use APSW exclusively, memory databases will work again.

---

## Bug 2: Concurrent Sessions Cause "disk I/O error"

### Symptom

When two `SQLiteFxtrSession` instances are opened concurrently on the same
file-based database, and the inner session closes first, the outer session fails
with "disk I/O error" when trying to mark itself as garbage during teardown.

### Diagnosis

We created extensive experiments in `tests/test_sqlite_store/sqlite_store_experiments.py`
to isolate the root cause. Key findings:

**What works (does NOT trigger the bug):**
- Two APSW pool groups on same DB
- Two SQLAlchemy engines on same DB
- APSW pool + SQLAlchemy engine together
- Two sessions in the SAME task group
- Close only pool2 (not engine2)
- Dispose only engine2 (not pool2)
- No disposal at all

**What fails (triggers the bug):**
- Two sessions in NESTED task groups + dispose engine2 + close pool2

The issue only occurs when BOTH the SQLAlchemy engine disposal AND the APSW pool
closure happen while another session's lifecycle handler is still waiting.

**Root cause hypothesis:**

There's some interaction between:
- SQLAlchemy engine disposal (which may checkpoint WAL or close sqlite3 connections)
- APSW pool closure (which closes its separate SQLite connections)
- Having a background task using engine1 that's waiting

When both engine2 disposal AND pool2 closure happen while session1's lifecycle
handler is still waiting, something about the database file state becomes
corrupted for engine1's connections.

### Resolution

The fix was to migrate the session lifecycle operations from SQLAlchemy to APSW:

1. Added `sqlite_ops.insert_session()` - inserts a new session using APSW
2. Added `sqlite_ops.mark_session_garbage()` - marks a session as garbage using APSW
3. Modified `_lifecycle_handler()` in `sqlite_namespace.py` to use these APSW
   operations instead of SQLAlchemy

By using the APSW pool for session lifecycle operations, the lifecycle handler
no longer depends on the SQLAlchemy engine. This avoids the corruption issue
because the lifecycle handler now only uses APSW connections, which are unaffected
by SQLAlchemy engine disposal of other sessions.

**Files changed:**
- `fxtr2/data/sqlite_store/sqlite_ops.py` - Added `insert_session()` and `mark_session_garbage()`
- `fxtr2/data/sqlite_store/sqlite_namespace.py` - Modified `_lifecycle_handler()` to use APSW

---

## Experiments File

The detailed experiments used to diagnose Bug 2 are preserved in:
`tests/test_sqlite_store/sqlite_store_experiments.py`

This file is intentionally NOT prefixed with `test_` so pytest doesn't run it by
default. The tests can be run manually with:

```bash
uv run pytest tests/test_sqlite_store/sqlite_store_experiments.py -v
```

Many of the xfailed tests in that file now pass since the bug is fixed.
