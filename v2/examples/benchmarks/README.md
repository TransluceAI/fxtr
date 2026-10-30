# Fxtr2 Benchmarks

This directory contains benchmarks for measuring performance of various fxtr2 components.

## sqlite_benchmark.py

Compares performance between the old `sqlite_arena` implementation and the new `sqlite_store` (APSW-based) implementation.

### Usage

```bash
# Run full benchmark (compares old vs new)
uv run python examples/benchmarks/sqlite_benchmark.py

# Run only new implementation (useful for tracking regressions)
uv run python examples/benchmarks/sqlite_benchmark.py --skip-old

# Customize tree depth (default: 10, creates ~1024 entities)
uv run python examples/benchmarks/sqlite_benchmark.py --depth 12  # ~4096 entities
```

### Benchmark Scenarios

The benchmark measures three scenarios:

**Scenario A: Memory → SQLite**
- Creates a binary tree of entities in a MemoryArena
- Pins the tree to a SQLite namespace (copies all entities to SQLite)
- Measures total time to persist the tree

**Scenario B: SQLite → Memory**
- Retrieves the pinned tree from SQLite
- Loads it into a fresh MemoryStore (no shared backing storage)
- Measures total time to read the tree from SQLite

**Scenario C: Cross-Namespace Copy**
- Copies the tree from one namespace to another in the same SQLite database
- This shares entity data but creates new namespace metadata
- Measures total time to set up the second namespace

### Entity Structure

Each tree node contains:
- `value`: integer node ID
- `left`: optional child reference
- `right`: optional child reference
- `data`: 1KB random bytes payload

A depth-10 tree contains ~1024 entities with ~1MB total data.

### Expected Results

The new APSW-based implementation (`sqlite_store`) should be **2-3x faster** than the old SQLAlchemy-based implementation (`sqlite_arena`) due to:

1. Direct APSW access (no SQLAlchemy Core overhead)
2. Constant SQL strings (better SQLite query caching)
3. Sync operations in thread pool (no async overhead for blocking I/O)
4. Connection pooling with read/write separation (better WAL mode concurrency)

### Notes

- The benchmark uses temporary databases that are cleaned up automatically
- Tree creation time is not included in measurements (only copy operations)
- Results may vary based on disk speed, CPU, and system load
