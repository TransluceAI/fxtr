# Stage 8 Post-Implementation Follow-ups

This document tracks follow-up work after completing the APSW migration in stage8_sqlite_speedup.md.

## Goals

1. Optimize dynamic SQL in `load_precomp_data_for_entities` to use constant SQL with JSON
2. Migrate existing users from `sqlite_arena.py` to `sqlite_store/sqlite_namespace.py`
3. Deprecate `sqlite_arena.py` by moving to deprecated subfolder
4. Update documentation to reflect new primary implementation
5. Create benchmarks comparing old vs new implementation

## Step 1: Dynamic SQL Optimization (JSON-based)

**Goal**: Replace dynamic SQL with variable-length IN clauses in `load_precomp_data_for_entities` with constant SQL using SQLite's JSON functions.

**Current approach** (in `sqlite_ops.py`):
```python
placeholders = ",".join("?" * len(frontier_list))
cursor = conn.execute(
    f"SELECT id, public_id FROM entity WHERE id IN ({placeholders})",
    frontier_list,
)
```

**New approach** (using JSON):
```python
import json
cursor = conn.execute(
    """SELECT e.id, e.public_id FROM entity e
       WHERE e.id IN (SELECT value FROM json_each(?))""",
    (json.dumps(frontier_list),)
)
```

**Tasks**:
- [x] Update `load_precomp_data_for_entities` in `sqlite_ops.py` to use JSON-based queries
- [x] Verify all tests still pass (87 tests pass!)
- [x] Check SQLite version requirement (needs 3.38+ for json_each, released 2022-02)

**Answers**:
- SQLite version requirement: Documented in pyproject.toml (APSW bundles recent SQLite)
- No runtime version check needed (APSW controls the version)

## Step 2: Migrate Existing Users

**Goal**: Update all code that uses `sqlite_arena.py` to use `sqlite_store/sqlite_namespace.py` instead.

**Known users**:
1. `fxtr2/data/git_store/sync.py` - SQLite store for git-backed namespaces
2. `fxtr2/data/fxtr_context_api.py` - `SQLiteFxtrNamespace` wrapper

**Migration pattern**:
```python
# OLD:
from fxtr2.data.sqlite_arena import SQLiteFxtrSession, SQLiteNamespaceBackend
# NEW:
from fxtr2.data.sqlite_store import SQLiteFxtrSession, SQLiteNamespaceBackend
```

**Tasks**:
- [x] Update imports in `git_store/git_namespace.py`
- [x] Update imports in `git_store/sync.py`
- [x] Update imports in `fxtr_context_api.py`
- [x] Add URL conversion in `fxtr_context_api.py` (SQLAlchemy URLs → plain paths)
- [x] Search for any other imports of `sqlite_arena` across the codebase
- [x] Fix export/import tests database path issue (all 3 tests now pass)
- [x] Investigate and fix concurrent access test locking issue (RESOLVED - was APSW context manager bug)
- [x] Verify all affected tests pass (28/28 git_namespace tests now pass!)

**Answers**:
- Public API should be identical except for minor session creation differences (SQLAlchemy URIs → plain SQLite paths)
- git_store will only support the new backend; update tests to use sqlite_store only

## Step 3: Deprecate sqlite_arena.py

**Goal**: Move old implementation to deprecated subfolder to make the new implementation the source of truth.

**File moves**:
```
fxtr2/data/sqlite_arena.py → fxtr2/data/deprecated/sqlite_arena.py
fxtr2/data/sqlite_arena_migrations.py → fxtr2/data/deprecated/sqlite_arena_migrations.py
tests/test_sqlite_arena.py → tests/deprecated/test_sqlite_arena.py
tests/test_sqlite_arena_migrations.py → tests/deprecated/test_sqlite_arena_migrations.py
```

**Tasks**:
- [x] Create `fxtr2/data/deprecated/` directory with `__init__.py`
- [x] Create `tests/deprecated/` directory with `__init__.py`
- [x] Move sqlite_arena files to deprecated/
- [x] Add deprecation notice to moved files (docstring warning)
- [x] Update imports in deprecated files to point to deprecated/ locations
- [x] Verify deprecated tests still pass (75 tests pass!)

**Deprecation notice format**:
```python
"""
DEPRECATED: This module is deprecated in favor of fxtr2.data.sqlite_store.

Use fxtr2.data.sqlite_store.sqlite_namespace instead. This legacy implementation
is kept for reference and compatibility testing only.

See plans/phase2/stage8_sqlite_speedup.md for details on the new implementation.
"""
```

**Answers**:
- Deprecated tests can be skipped by default (old implementation not actively maintained)
- Clean break (no backward compatibility shims needed)

## Step 4: Update Documentation

**Goal**: Update CODE_OVERVIEW.md to reflect sqlite_store as the primary implementation.

**Changes needed**:
- [x] Update "sqlite_arena.py" section to point to deprecated version
- [x] Update file structure to show deprecated/ folder
- [x] Update test counts to include deprecated tests separately

**Typical usage example update**:
```python
# OLD:
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

async with SQLiteFxtrNamespace("sqlite+aiosqlite:///db.sqlite", "myns") as ns:
    ...

# NEW (same API, just clearer about the backend):
from fxtr2.data.sqlite_store import SQLiteFxtrSession
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

# Or use directly:
async with SQLiteFxtrSession.open("db.sqlite") as session:
    ns = await session.create_or_retrieve_user_namespace("myns")
    ...
```

## Step 5: Create Benchmarks

**Goal**: Quantify performance improvements with realistic workloads.

**Location**: `examples/benchmarks/sqlite_benchmark.py`

**Benchmark scenarios**:

### Scenario A: Copy large entity FROM memory INTO sqlite namespace
- Create a large binary tree of entities in a MemoryArena
  - Tree depth: configurable (e.g., 10 levels)
  - Each node is a small entity with references to left/right children
  - Total entities: ~2^depth
- Create fresh SQLite database with namespace
- Copy the root entity from memory arena to sqlite namespace (via pin)
- Measure: total time, time per entity

### Scenario B: Copy large entity FROM sqlite namespace INTO fresh memory
- Use the database created in Scenario A
- Create fresh MemoryStore (no shared backing storage)
- Retrieve pinned entity from sqlite namespace
- Copy to memory arena in the fresh store
- Measure: total time, time per entity

### Scenario C: Copy large entity between namespaces in same SQLite store
- Use the database with pinned entity from Scenario A
- Create second namespace in same SQLite database
- Copy entity from namespace 1 to namespace 2 (shares entity data, new namespace metadata)
- Measure: total time, time per entity

**Implementation details**:
- Run each scenario with both implementations:
  - Old: `fxtr2.data.deprecated.sqlite_arena`
  - New: `fxtr2.data.sqlite_store.sqlite_namespace`
- Use temporary directories for databases (cleanup after)
- Print comparison table with speedup ratios
- Consider using pytest-benchmark if we want to track regressions over time

**Tasks**:
- [x] Create `examples/benchmarks/` directory
- [x] Implement binary tree entity structure
- [x] Implement benchmark harness (setup, run, measure, report)
- [x] Implement Scenario A
- [x] Implement Scenario B
- [x] Implement Scenario C
- [x] Add README.md in benchmarks/ explaining how to run
- [x] Run benchmarks and document results in this plan

**Benchmark Results (depth=10, 1023 entities, ~1MB data):**
```
Scenario                   Old (s)    New (s)    Speedup
------------------------------------------------------
A: Memory → SQLite          10.393     1.387      7.49x
B: SQLite → Memory           8.692     1.181      7.36x
C: Cross-namespace           9.689     0.936     10.35x
```

The new APSW-based implementation is **7-10x faster** than the old SQLAlchemy implementation!
This far exceeds our target of 2-3x speedup.

**Answers**:
- Tree depth: 10 (produces ~1024 entities)
- Entity structure: Include 1KB random data payload per node
- WAL mode: Use default (WAL enabled for both implementations)
- Tracking: Write in a future-looking way; allow optionally disabling old impl to compare only new impl across changes

**Entity structure**:
```python
@dataclass(frozen=True)
class TreeNode(DataclassEntity):
    value: int
    left: ChildRef[TreeNode] | None
    right: ChildRef[TreeNode] | None
    data: bytes  # 1KB random payload
```

## Implementation Order (COMPLETED)

1. ✅ Step 1: Dynamic SQL optimization (quick win, improves performance)
2. ✅ Step 2: Migrate existing users (validates API compatibility)
3. ✅ Concurrent deadlock fix: Fixed APSW context manager issue
4. ✅ Step 5: Create benchmarks (validates performance - 7-10x speedup achieved!)
5. ✅ Step 3: Deprecate old implementation (moved to deprecated/)
6. ✅ Step 4: Update documentation (CODE_OVERVIEW.md updated)

## Concurrent Access Deadlock Issue (RESOLVED)

**Issue discovered**: When migrating git_store tests to use sqlite_store, the `test_two_namespaces_see_each_others_writes` test failed with a database locking deadlock.

**Root cause**: APSW's `with conn:` context manager causes deadlocks when:
- `conn.transaction_mode` is set (e.g., to "IMMEDIATE")
- Multiple connections try to start transactions concurrently
- The connection that enters successfully blocks for the busy timeout duration
- Other connections get `SQLITE_BUSY` errors

**Reproduction**: Created minimal test in `examples/sqlite_debugging/sqlite_deadlock_debugging.py` that demonstrates:
- Test 1 (explicit `BEGIN`/`COMMIT`): ✅ Works correctly
- Test 2 (`conn.transaction_mode` + `with conn:`): ❌ Deadlocks

**Fix**: Updated `transaction()` in `sqlite_util.py` to use explicit `BEGIN` / `COMMIT` / `ROLLBACK` instead of relying on APSW's context manager.

**Result**: All tests pass including the previously failing concurrent access test.

## Success Criteria

- [x] All tests pass with new implementation (87 sqlite_store tests + 28 git_namespace tests)
- [x] Benchmarks show measurable improvement (7-10x speedup achieved!)
- [x] No regressions in functionality
- [x] Concurrent access deadlock issue resolved
- [x] Documentation is clear and up-to-date
- [x] Code is cleaner (old implementation properly deprecated)

## Notes

- The json_each approach is well-supported (SQLite 3.38+, Feb 2022)
- Python 3.12 ships with SQLite 3.45+ so we should be fine for version requirements
- APSW links its own SQLite version, so we control the version
