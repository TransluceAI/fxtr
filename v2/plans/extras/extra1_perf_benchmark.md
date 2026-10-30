# Plan: Performance Benchmark for Data Storage Transfers

**Goal**: Build a configurable benchmark system for measuring the performance of transferring entities between scopes and namespaces. Replaces the existing `examples/benchmarks/sqlite_benchmark.py`.

**Location**: `examples/benchmarks/store_benchmarks/`

See `extra1_perf_benchmark_scratch.md` for design rationale and scratch thinking.

## Architecture

### Overview

A custom benchmark runner (no pytest-benchmark) with three layers:

1. **Entity generators** — produce entities in a source scope, return `(key, entity_id)` pairs
2. **Benchmark cases** — each case declares a name, entity generator, and async target factory; the core runner doesn't know about SQLite vs memory
3. **Runner** — filters cases by keyword, runs each N times, collects timing, prints table, optionally saves JSON and/or runs viztracer

### File layout

```
examples/benchmarks/store_benchmarks/
├── __init__.py
├── run.py                # CLI entry point
├── runner.py             # Core timing loop + reporting
├── entity_generators.py  # Pluggable entity shape generators
└── cases.py              # Benchmark case definitions
```

### Core types

```python
@dataclass
class BenchmarkCase:
    name: str
    description: str
    setup: Callable that returns a BenchmarkState
    run: Callable that takes BenchmarkState and does the timed work
    teardown: Callable that cleans up after each iteration
    global_setup: Callable for one-time setup (source entities)
    global_teardown: Callable for one-time teardown

@dataclass
class BenchmarkState:
    """Opaque state passed from setup → run → teardown."""
    source_scope: FxtrScope
    entity_keys: list[tuple[str, FxtrID]]
    target: Any  # namespace or scope, case-specific
    tmp_dir: Path  # per-iteration temp dir (for target DBs)
    ... # case-specific extras
```

Actually, to keep this simple and flexible, each case is just a class with async context managers for setup/teardown and an async `run()` for the timed operation:

```python
class SomeBenchmarkCase:
    name = "memory_to_sqlite"
    description = "Pin entities from memory scope into SQLite namespace"

    @contextlib.asynccontextmanager
    async def global_context(self, entity_depth: int, payload_size: int) -> AsyncIterator[None]:
        """One-time setup/teardown: create source entities, yield, then clean up."""
        async with FxtrScope() as scope:
            ...  # create entities
            yield
        # scope exits cleanly here

    @contextlib.asynccontextmanager
    async def iteration_context(self) -> AsyncIterator[None]:
        """Per-iteration setup/teardown (not timed): create fresh target, yield, then clean up."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            async with SQLiteFxtrNamespace(...) as ns:
                self._namespace = ns
                yield

    async def run(self) -> None:
        """The timed operation."""
```

The runner calls these as: `async with global_context(): for each iteration: async with iteration_context(): run()`. Only `run` is timed. Using context managers instead of explicit setup/teardown methods ensures proper cleanup of async resources (scopes, namespaces) even on errors.

### Entity generators

```python
async def generate_binary_tree(
    scope: FxtrScope, depth: int, payload_size: int = 1024
) -> list[tuple[str, FxtrID]]:
    """Create a complete binary tree and return (key, id) pairs for all nodes."""
```

Returns a flat list of `(key, entity_id)` pairs suitable for pinning. Keys are like `"node/0"`, `"node/1"`, etc. The generator stores entities into the given scope and returns the IDs.

Initially just the binary tree. The interface is simple enough that adding flat collections, deep chains, etc. later is straightforward.

### Benchmark cases

#### Case A: Memory → SQLite (pin)

- **global_setup**: Create a `FxtrScope`, generate entities into it via the entity generator. Keep the scope open.
- **setup**: Create a temp directory, open a `SQLiteFxtrNamespace` pointing at a fresh DB in that temp dir.
- **run**: For each `(key, entity_id)` in the source, call `await namespace.pin(key, entity_id, scope=source_scope)`.
- **teardown**: Close the namespace, remove the temp directory.
- **global_teardown**: Close the source scope.

#### Case B: SQLite → Memory (copy)

- **global_setup**: Create a `FxtrScope`, generate entities, open a `SQLiteFxtrNamespace`, pin all entities into it. Then **close the namespace and the source scope** so that nothing is cached in memory. Keep the DB path and the list of keys.
- **setup**: Re-open the `SQLiteFxtrNamespace` at the saved DB path (cold open, no in-memory caches). Create a fresh `FxtrScope(isolated=True)` as the target.
- **run**: For each key, `await source_namespace.retrieve(key, into_scope=target_scope, copy_into_scope=True)`.
- **teardown**: Close the target scope and the re-opened namespace.
- **global_teardown**: Clean up temp DB.

#### Case D: Memory → SQLite with 20 indexers (pin)

- **global_setup**: Create a `FxtrScope`, generate entities. Pre-build 20 `TypedPathValueIndexer` objects (indexing different synthetic paths on the entity type).
- **setup**: Create a temp dir, open a fresh `SQLiteFxtrNamespace`, register all 20 indexers (not timed).
- **run**: Pin all entities (indexers fire during pin, building index entries).
- **teardown**: Close namespace, remove temp dir.
- **global_teardown**: Close source scope.

For the 20 indexers: the entity type needs fields that the indexers can actually index. The binary tree has `value: int` and `data: bytes`. We could either:
- (a) Add more fields to the benchmark entity type (e.g. `field_0: int`, `field_1: float`, ..., `field_19: str`)
- (b) Have some indexers index the same paths with different variable structures

Option (a) is more realistic. We'll define a `RichTreeNode` entity with 20 indexable fields, and create 20 `TypedPathValueIndexer` instances each targeting a different field. Each indexer is simple (no variables, just a path key + value type). The entity generator for this case produces `RichTreeNode` entities.

### Runner

```python
async def run_benchmark(
    cases: list[BenchmarkCaseProtocol],
    iterations: int = 5,
    keyword: str | None = None,
    json_output: Path | None = None,
    trace_case: str | None = None,
    entity_count_hint: int = 1023,
) -> None:
```

- Filters cases by keyword (substring match on `name`)
- If `trace_case` is set: run exactly that case once under viztracer, save trace, return
- Otherwise: for each case, run the timing loop, collect `list[float]` of durations
- Print a summary table with min / median / max / entity count / entities/sec
- Optionally save results to JSON

### viztracer integration

When `--trace CASE_NAME` is passed:

```python
from viztracer import VizTracer

tracer = VizTracer(output_file=f"{case_name}.json")
tracer.start()
await case.global_setup(entity_count)
await case.setup()
await case.run()
await case.teardown()
await case.global_teardown()
tracer.stop()
tracer.save()
```

This gives a clean single-run trace of the entire operation, including setup/teardown so you can see the full picture. The trace file can be opened in `vizviewer`.

### CLI

```
uv run python -m examples.benchmarks.store_benchmarks.run [OPTIONS]

# or more simply:
uv run python examples/benchmarks/store_benchmarks/run.py [OPTIONS]
```

Options:
- `-k PATTERN` — only run cases whose name contains PATTERN
- `-n NUM` — number of iterations (default: 5)
- `--trace NAME` — run one case under viztracer, save to `{NAME}.json`
- `--json FILE` — save results to JSON file
- `--entity-depth N` — binary tree depth, controls entity count (default: 10, giving 1023 entities)

## Implementation steps

- [x] 1. Create `examples/benchmarks/store_benchmarks/` directory with `__init__.py`
- [x] 2. Implement `entity_generators.py`: define `BenchmarkTreeNode` and `RichBenchmarkTreeNode` entity types, implement `generate_binary_tree()` and `generate_rich_binary_tree()` functions
- [x] 3. Implement `runner.py`: define the `BenchmarkCase` protocol, timing loop, table printing, JSON output, viztracer integration
- [x] 4. Implement `cases.py`: the three benchmark cases (A: memory→sqlite, B: sqlite→memory, D: memory→sqlite+indexers)
- [x] 5. Implement `run.py`: CLI entry point with argparse, case registration, call into runner
- [x] 6. Test by running and verifying output looks reasonable
- [x] 7. Test viztracer mode (requires `uv sync --group prof`)
- [x] 8. Test keyword filtering
- [x] 9. Remove old `examples/benchmarks/sqlite_benchmark.py` and update `examples/benchmarks/README.md`
- [x] 10. Update CODE_OVERVIEW.md

## Phase 2: Entity scenario × Transfer scenario matrix

The initial implementation couples entity generation and transfer direction into a single class per case. This makes it hard to separately test different entity shapes (one big recursive tree vs many small independent objects) against different transfer directions. It also makes the `--entity-depth` / `--payload-size` CLI flags the only way to vary workloads, which doesn't capture the fundamental difference between "1 pin moving 2047 entities recursively" vs "500 pins each moving 1 entity."

### New abstractions

**EntityScenario** — a named, fully-configured entity workload with no parameters:

```python
@dataclass(frozen=True)
class EntityScenario:
    name: str
    description: str
    generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]]
    # generate() is called inside an active scope.
    # Returns (pin_keys_and_ids, total_entity_count).
```

Each scenario is a fixed configuration — "depth-10 tree with 1KB payload" is one scenario, "500 independent 1KB entities" is another. No parameterization; to vary the size, add a new scenario. Concrete initial scenarios:

| Name | Description | Pins | Entities |
|------|-------------|------|----------|
| `single_tree_1k` | Depth-10 binary tree, 1KB payload | 1 | 2047 |
| `many_flat_1k` | 500 independent flat entities, 1KB each | 500 | 500 |
| `rich_tree_1k` | Depth-10 tree with 20 indexable fields, 1KB payload | 1 | 2047 |

**TransferScenario** — owns the full source/target lifecycle, receives entity generation as a callback:

```python
class TransferScenario(Protocol):
    name: str
    description: str

    def global_context(
        self,
        generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]],
    ) -> AbstractAsyncContextManager[None]: ...

    def iteration_context(self) -> AbstractAsyncContextManager[None]: ...
    async def run(self) -> None: ...

    @property
    def entity_count(self) -> int: ...
    @property
    def pin_count(self) -> int: ...
```

The transfer scenario calls `generate()` inside whatever scope it creates, then does source setup (e.g. pin into sqlite for the sqlite→memory case), then yields. This gives it full control over the source lifecycle — critical for the sqlite→memory case where we want to close the memory scope after populating the DB so there are no caching effects.

Concrete transfer scenarios (same 3 as before, just factored out):

| Name | Source | Target | Notes |
|------|--------|--------|-------|
| `memory_to_sqlite` | Memory scope | Fresh SQLite namespace | Pin operation |
| `sqlite_to_memory` | Pre-populated SQLite namespace | Isolated memory scope | Cold open per iteration |
| `memory_to_sqlite_indexed` | Memory scope | Fresh SQLite namespace + 20 indexers | Pin with index computation |

**BenchmarkCase** — just pairs an entity scenario with a transfer scenario:

```python
@dataclass
class BenchmarkCase:
    entity: EntityScenario
    transfer: TransferScenario

    @property
    def name(self) -> str:
        return f"{self.entity.name}/{self.transfer.name}"
```

Case names become `entity/transfer`, e.g. `single_tree_1k/memory_to_sqlite`. The `-k` flag filters on this combined name, so `-k memory_to_sqlite` runs all entity shapes against that transfer, and `-k single_tree` runs that shape against all transfers.

### Matrix curation

Not all pairings make sense. The `memory_to_sqlite_indexed` transfer requires `RichBenchmarkTreeNode` entities (it has indexers targeting `f_int_*` / `f_float_*` fields). So the matrix is curated:

```python
ALL_CASES = [
    # single_tree_1k × all non-indexed transfers
    BenchmarkCase(single_tree_1k, memory_to_sqlite),
    BenchmarkCase(single_tree_1k, sqlite_to_memory),
    # many_flat_1k × all non-indexed transfers
    BenchmarkCase(many_flat_1k, memory_to_sqlite),
    BenchmarkCase(many_flat_1k, sqlite_to_memory),
    # rich_tree_1k × indexed transfer
    BenchmarkCase(rich_tree_1k, memory_to_sqlite_indexed),
]
```

### Reporting changes

The results table gains a `Pins` column to distinguish "1 pin moving 2047 entities" from "500 pins moving 500 entities":

```
Case                                        Entities    Pins   Min (s)   ...
single_tree_1k/memory_to_sqlite                 2047       1    0.1632   ...
many_flat_1k/memory_to_sqlite                    500     500    0.0843   ...
```

### CLI changes

Remove `--entity-depth` and `--payload-size` flags (scenarios are pre-configured). The `-k`, `-n`, `--json`, and `--trace` flags stay the same.

### File changes

- **`entity_generators.py`**: Entity types unchanged. Generator functions become zero-arg callables. Add `many_flat_1k` generator for independent flat entities. Wrap each in an `EntityScenario` dataclass. Export the scenario instances.
- **`cases.py`**: Current `MemoryToSqlite` / `SqliteToMemory` / `MemoryToSqliteIndexed` classes become `TransferScenario` implementations. `global_context` takes a `generate` callback instead of `entity_depth`/`payload_size`. The matrix of entity × transfer combinations is defined here as `ALL_CASES`.
- **`runner.py`**: `BenchmarkCase` protocol changes to have `entity: EntityScenario` and `transfer: TransferScenario`. `run_case` calls `transfer.global_context(entity.generate)`. Remove `entity_depth`/`payload_size` from all signatures. Add `pin_count` to `BenchmarkResult` and the table.
- **`run.py`**: Remove `--entity-depth` and `--payload-size` args.

### Implementation steps

- [x] 1. Add `EntityScenario` dataclass and `many_flat_1k` generator to `entity_generators.py`; wrap existing generators in `EntityScenario` instances
- [x] 2. Refactor transfer scenarios in `cases.py`: rename current classes, change `global_context` to accept `generate` callback
- [x] 3. Add `BenchmarkCase` dataclass to `cases.py`; build the curated matrix in `ALL_CASES`
- [x] 4. Update `runner.py`: change `BenchmarkCase` protocol, `run_case`, `run_case_with_trace`, `run_benchmarks` signatures; add `pin_count` to reporting
- [x] 5. Update `run.py`: remove `--entity-depth` / `--payload-size` flags
- [x] 6. Test all cases run correctly
- [x] 7. Update README.md and CODE_OVERVIEW.md

## Phase 3: Flat Names + Subcommand CLI

Decoupled case naming from the entity×transfer structure (sparse matrix made compound names misleading), and replaced flat flags with `run`/`trace`/`list` subcommands sharing a unified `-k` filter.

### Changes

- `BenchmarkCase` takes an explicit `name` instead of deriving `entity.name/transfer.name`
- CLI uses subcommands: `run [-k PAT] [-n NUM] [--json FILE]`, `trace [-k PAT] [-o DIR]`, `list [-k PAT]`
- `runner.py` exports `filter_cases`, `list_cases`, `trace_cases`; `run_benchmarks` no longer handles filtering or trace dispatch
- `trace` supports multiple cases, saves each to `{output_dir}/{case_name}.json`

### Implementation steps

- [x] 1. Update `BenchmarkCase` in `cases.py` to take explicit `name`, update `ALL_CASES`
- [x] 2. Add `filter_cases` and `list_cases` to `runner.py`
- [x] 3. Simplify `run_benchmarks` signature (remove keyword/trace_case)
- [x] 4. Refactor `run_case_with_trace` → `trace_cases` (takes dir, loops over cases)
- [x] 5. Rewrite `run.py` with subcommands (`run`, `trace`, `list`) + shared `-k`
- [x] 6. Test: `list`, `run -k tree`, `trace -k tree_pin`
- [x] 7. Update CODE_OVERVIEW.md
